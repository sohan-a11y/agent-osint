"""CommentForensics service: resumable docket ingestion + campaign detection."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from ..core.http import FetchError
from ..core.privacy import WITHHELD, safe_name, scrub_all
from ..core.results import ToolInputError, ok
from ..core.runtime import Runtime
from .cluster import cluster_comments, display_text
from .sources import CommentRecord, Mirrulations, RegulationsGov, read_bulk_csv
from .sponsors import sponsor_hints
from .store import CommentStore

_DOCKET_RE = re.compile(r"^[A-Z][A-Z0-9]{1,15}(?:-[A-Za-z0-9]{1,15}){1,5}$")
COMMENT_URL = "https://www.regulations.gov/comment/{}"
LIMITATIONS = [
    "Identical or near-identical text shows coordination (form letters), which is lawful and common; it is not "
    "evidence of fraud or fake identities.",
    "Comments whose content is only in attachments (PDF/DOCX) are counted but not clustered.",
    "Agencies may post a single representative copy of mass mail campaigns with a 'duplicate comments' count; "
    "sizes including those counts are reported separately.",
    "Commenter names are never returned; text excerpts have names, emails and phone numbers masked.",
]


class CommentForensics:
    def __init__(self, rt: Runtime):
        self.rt = rt
        self.store = CommentStore(rt.settings.data_dir("commentforensics") / "comments.sqlite")
        self.api = RegulationsGov(rt.fetcher, rt.settings.regulations_gov_api_key)
        self.mirror = Mirrulations(rt.fetcher)

    @staticmethod
    def _check_docket(docket_id: str) -> str:
        d = docket_id.strip()
        if not _DOCKET_RE.match(d):
            raise ToolInputError(f"{docket_id!r} is not a Regulations.gov docket ID", hint="e.g. 'EPA-HQ-OAR-2021-0317'")
        return d

    # ---------------------------------------------------------------- overview
    async def docket_overview(self, docket_id: str) -> dict[str, Any]:
        d = self._check_docket(docket_id)
        evidence = []
        warnings = []
        data: dict[str, Any] = {"docket_id": d, "local": self.store.counts(d)}
        if self.api.api_key:
            docket, res = await self.api.docket(d)
            evidence.append(self.rt.evidence.record(res, note="Regulations.gov docket"))
            a = (docket.get("data") or {}).get("attributes") or {}
            data["docket"] = {k: a.get(k) for k in ("title", "agencyId", "docketType", "modifyDate", "rin", "keywords")}
            docs, results = await self.api.documents(d)
            evidence += [self.rt.evidence.record(r, note="Regulations.gov documents list") for r in results]
            self.store.save_documents(d, docs)
            for doc in self.store.documents(d)[:15]:
                if doc["object_id"]:
                    total, res = await self.api.comment_count(doc["object_id"])
                    self.store.set_document_total(d, doc["object_id"], total)
            data["documents"] = self.store.documents(d)
            data["total_comments_reported"] = sum((x["total_comments"] or 0) for x in data["documents"])
        else:
            warnings.append("REGULATIONS_GOV_API_KEY not set: docket metadata unavailable; ingestion can use the "
                            "Mirrulations mirror (source='mirrulations') or a bulk CSV instead.")
        data["ingest_state"] = {k: self.store.get_state(d, k) for k in ("source", "phase")}
        return ok(data, evidence=evidence, warnings=warnings)

    # ---------------------------------------------------------------- ingestion
    async def ingest(self, docket_id: str, *, source: str = "auto", max_requests: int = 150) -> dict[str, Any]:
        d = self._check_docket(docket_id)
        if source not in {"auto", "api", "mirrulations"}:
            raise ToolInputError("source must be 'auto', 'api' or 'mirrulations'")
        if source == "auto":
            stored = self.store.get_state(d, "source")
            if stored in {"api", "mirrulations"} and (stored != "api" or self.api.api_key):
                source = stored
            else:
                source = "api" if self.api.api_key else "mirrulations"
        self.store.set_state(d, "source", source)
        budget = {"left": max(1, min(max_requests, 2000)), "used": 0}
        warnings: list[str] = []
        try:
            if source == "api":
                phase = await self._ingest_api(d, budget, warnings)
            else:
                phase = await self._ingest_mirror(d, budget, warnings)
        except FetchError as exc:
            warnings.append(f"stopped early: {exc}")
            phase = self.store.get_state(d, "phase", "incomplete")
        counts = self.store.counts(d)
        pending = counts["known_comments"] - counts["fetched"]
        self.store.set_state(d, "phase", phase)
        rate = 1000 if source == "api" else 3600 * self.rt.settings.host_rps.get("mirrulations.s3.amazonaws.com", 2.0)
        data = {
            "docket_id": d, "source": source, "phase": phase, "requests_used": budget["used"],
            **counts, "pending_text_fetches": pending,
            "complete": phase == "complete",
            "estimated_hours_remaining": round(pending / rate, 2) if pending else 0,
            "mismatched_records_excluded": self.store.mismatches(d),
        }
        if phase != "complete":
            data["next_step"] = "Call ingest_docket again to continue; progress is saved."
        return ok(data, warnings=warnings)

    def _spend(self, budget: dict[str, int]) -> bool:
        if budget["left"] <= 0:
            return False
        budget["left"] -= 1
        budget["used"] += 1
        return True

    async def _ingest_api(self, d: str, budget: dict[str, int], warnings: list[str]) -> str:
        if not self.store.get_state(d, "docs_done"):
            if not self._spend(budget):
                return "documents"
            docs, results = await self.api.documents(d)
            budget["used"] += len(results) - 1
            self.store.save_documents(d, docs)
            self.store.set_state(d, "docs_done", True)
            if not docs:
                warnings.append("docket has no documents (check the docket ID)")
                return "complete"
        listing: dict[str, dict] = self.store.get_state(d, "listing", {})
        for doc in self.store.documents(d):
            oid = doc["object_id"]
            if not oid:
                continue
            st = listing.setdefault(oid, {"page": 1, "since": None, "done": False, "last_since": None})
            while not st["done"]:
                if not self._spend(budget):
                    self.store.set_state(d, "listing", listing)
                    return "listing"
                payload, _res = await self.api.comment_list_page(oid, st["page"], st["since"])
                items = payload.get("data") or []
                meta = payload.get("meta") or {}
                if st["page"] == 1 and st["since"] is None:
                    self.store.set_document_total(d, oid, meta.get("totalElements"))
                self.store.upsert_listed(d, items, doc["document_id"])
                if not meta.get("hasNextPage"):
                    st["done"] = True
                elif st["page"] >= 20:
                    last = (items[-1].get("attributes") or {}).get("lastModifiedDate") if items else None
                    if not last or last == st.get("last_since"):
                        warnings.append(f"cannot advance past {last} for {oid}; >5000 comments share one timestamp")
                        st["done"] = True
                    else:
                        st["last_since"] = st["since"] = last
                        st["page"] = 1
                else:
                    st["page"] += 1
            self.store.set_state(d, "listing", listing)
        self.store.set_state(d, "listing", listing)
        for cid in self.store.pending_detail(d, 100000):
            if not self._spend(budget):
                return "text"
            try:
                data, res = await self.api.comment_detail(cid)
            except FetchError as exc:
                if exc.status == 404:
                    self.store.upsert_full(d, CommentRecord(cid, None, d, None, None, None, None, None, None,
                                                            False, True, None, "api_missing"))
                    continue
                raise
            rec = CommentRecord.from_v4(data, "api")
            if rec.docket_id and rec.docket_id != d:
                self.store.add_mismatch(d)
                self.store.upsert_full(d, CommentRecord(cid, None, d, None, None, None, None, None, None, False,
                                                        True, None, "mismatch_excluded"))
                continue
            ev = self.rt.evidence.record(res, note=f"Regulations.gov comment {cid}", keep_body=False)
            self.store.upsert_full(d, rec, ev.id)
        return "complete"

    async def _ingest_mirror(self, d: str, budget: dict[str, int], warnings: list[str]) -> str:
        if not self.store.get_state(d, "mirror_listing_done"):
            after = self.store.get_state(d, "mirror_after")
            async for keys, _token in self.mirror.list_comment_keys(d, start_after=after, max_pages=1000):
                budget["left"] -= 1
                budget["used"] += 1
                items = [{"id": k.rsplit("/", 1)[1][:-5], "attributes": {}} for k in keys]
                self.store.upsert_listed(d, items, "")
                if keys:
                    self.store.set_state(d, "mirror_after", keys[-1])
                if budget["left"] <= 0:
                    return "listing"
            self.store.set_state(d, "mirror_listing_done", True)
            if self.store.counts(d)["known_comments"] == 0:
                warnings.append("Mirrulations has no comments for this docket (not mirrored, or no comments).")
                return "complete"
        prefix = Mirrulations.prefix(d)
        for cid in self.store.pending_detail(d, 100000):
            if not self._spend(budget):
                return "text"
            data, res = await self.mirror.comment(f"{prefix}{cid}.json")
            rec = CommentRecord.from_v4(data, "mirrulations")
            rec.comment_id = cid  # keep the mirror's disambiguated key
            if rec.docket_id and rec.docket_id != d:
                self.store.add_mismatch(d)
                continue
            ev = self.rt.evidence.record(res, note=f"Mirrulations mirror of comment {cid}", keep_body=False)
            self.store.upsert_full(d, rec, ev.id)
        return "complete"

    def ingest_csv(self, path: str, docket_id: str | None = None) -> dict[str, Any]:
        p = self.rt.settings.check_file(path, (".csv",))
        if not p.is_file():
            raise ToolInputError(f"file not found: {path}", hint="Path is on the machine running the server.")
        records, mapping = read_bulk_csv(p)
        if not records:
            raise ToolInputError("no comment rows found in CSV")
        dockets = Counter(r.docket_id for r in records if r.docket_id)
        d = docket_id or (dockets.most_common(1)[0][0] if dockets else None)
        if not d:
            raise ToolInputError("CSV has no Docket ID column; pass docket_id explicitly")
        d = self._check_docket(d)
        ev = self.rt.evidence.record_derived(f"file://{p.resolve()}", note="Regulations.gov bulk export CSV",
                                             payload=p.read_bytes())
        n = 0
        for r in records:
            if r.docket_id and r.docket_id != d:
                continue
            self.store.upsert_full(d, r, ev.id)
            n += 1
        self.store.set_state(d, "source", "bulk_csv")
        self.store.set_state(d, "phase", "complete")
        return ok({"docket_id": d, "rows_loaded": n, "column_mapping": mapping, **self.store.counts(d)},
                  evidence=[ev])

    # ---------------------------------------------------------------- analysis
    def _clusters(self, d: str, threshold: float, min_size: int) -> dict[str, Any]:
        if not 0.5 <= threshold <= 1.0:
            raise ToolInputError("threshold must be between 0.5 and 1.0")
        rows = self.store.texts(d)
        if not rows:
            raise ToolInputError(f"no comment text stored for {d}",
                                 hint="Run ingest_docket (or ingest_bulk_csv) first.")
        params = f"t={threshold}|m={min_size}"
        fp = self.store.fingerprint(d)
        cached = self.store.cached_clusters(d, params, fp)
        if cached:
            return cached
        res = cluster_comments(rows, threshold=threshold, min_size=min_size)
        out = {"n_total": res.n_total, "n_clusterable": res.n_clusterable, "n_attachment_only": res.n_attachment_only,
               "n_unique_texts": res.n_unique_texts, "clusters": res.clusters}
        self.store.save_clusters(d, params, fp, out)
        return out

    def _public_cluster(self, c: dict[str, Any], *, include_members: int = 0) -> dict[str, Any]:
        out = {k: v for k, v in c.items() if k not in {"member_ids", "organizations", "representative_text"}}
        out["representative_text"] = scrub_all(c["representative_text"])
        out["representative_url"] = COMMENT_URL.format(c["representative_comment_id"].split("#")[0])
        orgs = Counter()
        for name, n in c.get("organizations", []):
            orgs[safe_name(name)] += n
        out["organizations"] = [(n, k) for n, k in orgs.most_common(8) if n != WITHHELD]
        if include_members:
            out["sample_comments"] = [{"comment_id": m, "url": COMMENT_URL.format(m.split("#")[0])}
                                      for m in c["member_ids"][:include_members]]
        return out

    def _coverage_warnings(self, d: str) -> list[str]:
        counts = self.store.counts(d)
        w = []
        if counts["known_comments"] > counts["fetched"]:
            w.append(f"Only {counts['fetched']} of {counts['known_comments']} known comments have text so far; "
                     "results are partial. Continue with ingest_docket.")
        return w

    def campaign_clusters(self, docket_id: str, *, threshold: float = 0.8, min_size: int = 5,
                          limit: int = 20) -> dict[str, Any]:
        d = self._check_docket(docket_id)
        res = self._clusters(d, threshold, 2)
        big = [c for c in res["clusters"] if c["size"] >= min_size]
        in_campaigns = sum(c["size"] for c in big)
        data = {
            "docket_id": d, "threshold": threshold, "min_size": min_size,
            "comments_analysed": res["n_clusterable"], "attachment_only_comments": res["n_attachment_only"],
            "unique_texts": res["n_unique_texts"], "campaigns": len(big),
            "comments_in_campaigns": in_campaigns,
            "share_in_campaigns": round(in_campaigns / res["n_clusterable"], 3) if res["n_clusterable"] else 0,
            "clusters": [self._public_cluster(c) for c in big[:limit]],
        }
        return ok(data, warnings=self._coverage_warnings(d), limitations=LIMITATIONS)

    def _find_cluster(self, d: str, cluster_id: str, threshold: float) -> dict[str, Any]:
        res = self._clusters(d, threshold, 2)
        for c in res["clusters"]:
            if c["cluster_id"] == cluster_id:
                return c
        raise ToolInputError(f"unknown cluster {cluster_id} for {d} at threshold {threshold}",
                             hint="Cluster IDs come from campaign_clusters with the same threshold.")

    def cluster_detail(self, docket_id: str, cluster_id: str, *, threshold: float = 0.8, sample: int = 10) -> dict[str, Any]:
        d = self._check_docket(docket_id)
        c = self._find_cluster(d, cluster_id, threshold)
        rows = {r["comment_id"]: r for r in self.store.texts(d)}
        per_day = Counter((rows[m]["posted_date"] or "")[:10] for m in c["member_ids"] if m in rows)
        data = self._public_cluster(c, include_members=min(max(sample, 1), 50))
        data["posted_per_day"] = dict(sorted(per_day.items()))
        return ok(data, limitations=LIMITATIONS)

    def sponsor_hints(self, docket_id: str, cluster_id: str, *, threshold: float = 0.8) -> dict[str, Any]:
        d = self._check_docket(docket_id)
        c = self._find_cluster(d, cluster_id, threshold)
        rows = {r["comment_id"]: r for r in self.store.texts(d)}
        members = [rows[m] for m in c["member_ids"] if m in rows]
        hints = sponsor_hints(members, len(members))
        warnings = [] if hints else ["No sponsor hints found in the text or organisation fields."]
        return ok({"docket_id": d, "cluster_id": cluster_id, "cluster_size": len(members), "hints": hints},
                  warnings=warnings,
                  limitations=["Hints are leads to verify (e.g. by finding the template on the named site), "
                               "not attributions."])

    def docket_summary(self, docket_id: str, *, threshold: float = 0.8, min_size: int = 5) -> dict[str, Any]:
        d = self._check_docket(docket_id)
        rows = self.store.texts(d)
        if not rows:
            raise ToolInputError(f"no comments stored for {d}", hint="Run ingest_docket first.")
        res = self._clusters(d, threshold, 2)
        big = [c for c in res["clusters"] if c["size"] >= min_size]
        per_day = Counter((r["posted_date"] or "")[:10] for r in rows if r["posted_date"])
        orgs = Counter(safe_name(r["organization"]) for r in rows if r["organization"])
        orgs.pop(WITHHELD, None)
        singletons = res["n_clusterable"] - sum(c["size"] for c in res["clusters"])
        data = {
            "docket_id": d, **self.store.counts(d), "attachment_only": res["n_attachment_only"],
            "unique_texts": res["n_unique_texts"],
            "unique_comments_not_in_any_group": singletons,
            "campaigns_at_min_size": len(big),
            "comments_in_campaigns": sum(c["size"] for c in big),
            "largest_campaigns": [{"cluster_id": c["cluster_id"], "size": c["size"],
                                   "excerpt": (scrub_all(c["representative_text"]) or "")[:240]} for c in big[:5]],
            "organizations_top": orgs.most_common(15),
            "comments_per_day": dict(sorted(per_day.items())),
        }
        return ok(data, warnings=self._coverage_warnings(d), limitations=LIMITATIONS)

    def search_comments(self, docket_id: str, phrase: str, *, limit: int = 20) -> dict[str, Any]:
        d = self._check_docket(docket_id)
        if len(phrase.strip()) < 4:
            raise ToolInputError("phrase must be at least 4 characters")
        rows = self.store.conn.execute(
            "SELECT comment_id, text FROM comments WHERE docket_id=? AND text LIKE ? LIMIT ?",
            (d, f"%{phrase.strip()}%", max(1, min(limit, 200)))).fetchall()
        total = self.store.conn.execute("SELECT COUNT(*) FROM comments WHERE docket_id=? AND text LIKE ?",
                                        (d, f"%{phrase.strip()}%")).fetchone()[0]
        hits = []
        for cid, text in rows:
            t = display_text(text)
            i = t.lower().find(phrase.strip().lower())
            snippet = t[max(0, i - 120): i + len(phrase) + 120] if i >= 0 else t[:240]
            hits.append({"comment_id": cid, "url": COMMENT_URL.format(cid.split("#")[0]), "snippet": scrub_all(snippet)})
        return ok({"docket_id": d, "phrase": phrase, "total_matches": total, "matches": hits},
                  warnings=self._coverage_warnings(d))
