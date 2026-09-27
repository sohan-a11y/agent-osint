"""ClaimTrail: where and when did a claim, page or image first appear, and has it been fact-checked?"""

from __future__ import annotations

import mimetypes
from datetime import datetime, timezone
from typing import Any

from ..core.http import FetchError, RobotsDisallowed
from ..core.results import MissingCredential, ToolInputError, ok
from ..core.runtime import Runtime
from ..core.wayback import cdx_query
from .sources import extract_claimreview, extract_page_dates, factcheck_search, read_c2pa

LIMITATIONS = [
    "'Earliest known' is bounded by archive and index coverage; a claim may have appeared earlier elsewhere.",
    "Page-declared dates are self-reported and can be wrong or edited; archive capture times are independent.",
    "Claimant names that may belong to individuals are withheld; see the fact-check article.",
]


def _norm_date(v: str | None) -> str | None:
    if not v:
        return None
    s = v.strip()
    for fmt in (None, "%a, %d %b %Y %H:%M:%S %Z", "%Y%m%d%H%M%S", "%Y-%m-%d", "%d/%m/%Y", "%B %d, %Y"):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00")) if fmt is None else datetime.strptime(s, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return None


class ClaimTrail:
    def __init__(self, rt: Runtime):
        self.rt = rt

    async def fact_checks(self, query: str, *, language: str | None = None, max_age_days: int | None = None) -> dict[str, Any]:
        if len(query.strip()) < 3:
            raise ToolInputError("query too short")
        claims, res = await factcheck_search(self.rt.fetcher, self.rt.settings.google_factcheck_api_key, query,
                                             language=language, max_age_days=max_age_days)
        ev = self.rt.evidence.record(res, note="Google Fact Check Tools claims:search")
        warnings = [] if claims else ["No fact-checks found. Absence of a fact-check says nothing about accuracy."]
        return ok({"query": query, "claims": claims}, evidence=[ev], warnings=warnings, limitations=LIMITATIONS[2:])

    async def claimreview_from_url(self, url: str) -> dict[str, Any]:
        res = await self.rt.fetcher.get(url, respect_robots=True, max_bytes=4 * 1024 * 1024)
        if res.status != 200:
            raise FetchError(f"HTTP {res.status}", url=url, status=res.status)
        ev = self.rt.evidence.record(res, note="fact-check article")
        reviews = extract_claimreview(res.text())
        warnings = [] if reviews else ["No schema.org ClaimReview markup found on this page."]
        return ok({"url": res.url, "claim_reviews": reviews}, evidence=[ev], warnings=warnings)

    async def url_timeline(self, url: str, *, fetch_archived_first: bool = True) -> dict[str, Any]:
        if not url.startswith(("http://", "https://")):
            raise ToolInputError("url must start with http:// or https://")
        events: list[dict[str, Any]] = []
        evidence = []
        warnings: list[str] = []
        bare = url.split("://", 1)[1]
        try:
            caps, res = await cdx_query(self.rt.fetcher, bare, limit=1)
            evidence.append(self.rt.evidence.record(res, note="Wayback CDX earliest capture"))
            last, res2 = await cdx_query(self.rt.fetcher, bare, limit=1, reverse=True)
            if caps:
                events.append({"at": caps[0].iso, "type": "first_archive_capture", "source": "Internet Archive",
                               "url": caps[0].replay_url, "evidence_id": evidence[-1].id})
            if last and (not caps or last[0].timestamp != caps[0].timestamp):
                events.append({"at": last[0].iso, "type": "latest_archive_capture", "source": "Internet Archive",
                               "url": last[0].replay_url})
            if caps and fetch_archived_first:
                arch_url = f"https://web.archive.org/web/{caps[0].timestamp}id_/{caps[0].original}"
                try:
                    ares = await self.rt.fetcher.get(arch_url, max_bytes=4 * 1024 * 1024)
                    if ares.status == 200:
                        aev = self.rt.evidence.record(ares, note="earliest archived copy (raw)")
                        evidence.append(aev)
                        for d in extract_page_dates(ares.text(), arch_url).dates:
                            nd = _norm_date(d["value"])
                            if nd and d["kind"] in {"published", "created", "published?"}:
                                events.append({"at": nd, "type": f"declared_{d['kind'].rstrip('?')}_date_in_archived_copy",
                                               "source": d["source"], "url": arch_url, "evidence_id": aev.id})
                except FetchError as exc:
                    warnings.append(f"could not fetch archived copy: {exc}")
        except FetchError as exc:
            warnings.append(f"Wayback Machine unavailable: {exc}")
        live = None
        try:
            lres = await self.rt.fetcher.get(url, respect_robots=True, max_bytes=4 * 1024 * 1024)
            lev = self.rt.evidence.record(lres, note="live page")
            evidence.append(lev)
            if lres.status == 200 and "html" in lres.content_type.lower():
                pd = extract_page_dates(lres.text(), lres.url, lres.headers.get("last-modified"))
                live = {"status": lres.status, "title": pd.title, "canonical": pd.canonical}
                for d in pd.dates:
                    nd = _norm_date(d["value"])
                    if nd:
                        events.append({"at": nd, "type": f"declared_{d['kind'].rstrip('?')}_date", "source": d["source"],
                                       "url": lres.url, "evidence_id": lev.id})
                for cr in extract_claimreview(lres.text()):
                    if _norm_date(cr.get("date_published")):
                        events.append({"at": _norm_date(cr["date_published"]), "type": "fact_check_published",
                                       "source": cr.get("fact_checker"), "rating": cr.get("rating"), "url": lres.url})
            else:
                live = {"status": lres.status}
                if lres.status in (404, 410):
                    events.append({"at": lres.fetched_at, "type": "page_gone", "source": f"HTTP {lres.status}", "url": url})
        except RobotsDisallowed:
            warnings.append("robots.txt disallows fetching the live page; relying on archives.")
        except FetchError as exc:
            warnings.append(f"live page unavailable: {exc}")
        events.sort(key=lambda e: e["at"])
        earliest = next((e for e in events if e["type"] in {"first_archive_capture"} or e["type"].startswith("declared_published")
                         or e["type"].startswith("declared_created")), None)
        return ok({"url": url, "live": live, "earliest_known": earliest, "events": events}, evidence=evidence,
                  warnings=warnings, limitations=LIMITATIONS[:2])

    async def image_provenance(self, source: str) -> dict[str, Any]:
        if source.startswith(("http://", "https://")):
            res = await self.rt.fetcher.get(source, max_bytes=25 * 1024 * 1024, cache_ttl=86400)
            if res.status != 200:
                raise FetchError(f"HTTP {res.status}", url=source, status=res.status)
            data = res.content
            mime = res.content_type.split(";")[0].strip() or mimetypes.guess_type(source)[0] or "image/jpeg"
            ev = self.rt.evidence.record(res, note="media file", keep_body=False)
        else:
            p = self.rt.settings.check_file(source)
            if not p.is_file():
                raise ToolInputError(f"file not found: {source}")
            data = p.read_bytes()
            mime = mimetypes.guess_type(p.name)[0] or "image/jpeg"
            ev = self.rt.evidence.record_derived(f"file://{p.resolve()}", note="media file", payload=None)
        c2 = read_c2pa(data, mime)
        warnings = []
        if c2["status"] == "no_manifest":
            warnings.append("No Content Credentials. Most images have none; absence says nothing about authenticity.")
        if c2.get("validation_issues"):
            warnings.append("Manifest has validation issues (e.g. untrusted signer); treat claims as unverified.")
        import hashlib

        return ok({"source": source, "mime": mime, "sha256": hashlib.sha256(data).hexdigest(), "c2pa": c2},
                  evidence=[ev], warnings=warnings,
                  limitations=["C2PA proves what the signer asserted, not that the content is true; stripped "
                               "metadata leaves no trace."])

    async def claim_timeline(self, *, claim: str | None = None, urls: list[str] | None = None,
                             language: str | None = None) -> dict[str, Any]:
        if not claim and not urls:
            raise ToolInputError("give a claim text and/or URLs where it appeared")
        events: list[dict[str, Any]] = []
        evidence: list[Any] = []
        warnings: list[str] = []
        per_url = []
        for u in (urls or [])[:10]:
            r = await self.url_timeline(u)
            if r.get("ok"):
                per_url.append({"url": u, "earliest_known": r["data"]["earliest_known"]})
                events += [{**e, "subject": u} for e in r["data"]["events"]]
                evidence += r.get("evidence", [])
                warnings += r.get("warnings", [])
        checks = []
        if claim:
            try:
                fc = await self.fact_checks(claim, language=language)
                checks = fc["data"]["claims"]
                evidence += fc.get("evidence", [])
                for c in checks:
                    if _norm_date(c.get("claim_date")):
                        events.append({"at": _norm_date(c["claim_date"]), "type": "claim_date_per_fact_checker",
                                       "source": ", ".join(r["publisher"] or "" for r in c["reviews"])})
                    for rv in c["reviews"]:
                        if _norm_date(rv.get("review_date")):
                            events.append({"at": _norm_date(rv["review_date"]), "type": "fact_check_published",
                                           "source": rv.get("publisher"), "rating": rv.get("rating"), "url": rv.get("url")})
            except MissingCredential as exc:
                warnings.append(f"fact-check search skipped: {exc.env_var} not set")
        events.sort(key=lambda e: e["at"])
        earliest = next((e for e in events if e["type"] in {"first_archive_capture", "claim_date_per_fact_checker"}
                         or e["type"].startswith("declared_published") or e["type"].startswith("declared_created")), None)
        return ok({"claim": claim, "earliest_known_appearance": earliest, "per_url": per_url, "fact_checks": checks,
                   "timeline": events}, evidence=evidence[:20], warnings=warnings, limitations=LIMITATIONS)
