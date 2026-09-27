"""Procurement red-flags service."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import Counter
from dataclasses import asdict
from typing import Any

from ..core.http import FetchError, utcnow_iso
from ..core.privacy import WITHHELD, classify_name
from ..core.results import ToolInputError, ok
from ..core.runtime import Runtime
from .checks import Registries, Sanctions, norm_company
from .ocds import AwardRecord, compile_awards, iter_releases, parse_json_text
from .rules import RULES, SEVERITY_ORDER, RuleParams, _dt, aggregate_flags, award_flags, decorate, supplier_key

FTS_URL = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
LIMITATIONS = [
    "Red flags are risk indicators for further review, not evidence of corruption or wrongdoing.",
    "Results reflect only the data loaded into this server; incomplete publication distorts indicators.",
    "Names of suppliers that appear to be individuals (sole traders) are withheld.",
]


# org-id.guide schemes that only identify registered legal entities (companies, charities, LEIs).
ENTITY_REGISTRY_SCHEMES = {"GB-COH", "GB-CHC", "GB-SC", "GB-NIC", "XI-LEI", "XI-GLEIF", "NL-KVK", "FR-RCS",
                           "FR-SIREN", "DE-CR", "IE-CRO", "BE-BCE_KBO", "DK-CVR", "NO-BRC", "SE-BLV", "FI-PRO",
                           "US-EIN", "CA-CRA_ACR", "AU-ABN", "NZ-NZBN", "IN-MCA", "ZA-CPI"}


def looks_like_person(s: dict) -> bool:
    if (s.get("scheme") or "").upper() in ENTITY_REGISTRY_SCHEMES and s.get("identifier"):
        return False
    if s.get("legal_name") and classify_name(s["legal_name"]) == "organization":
        return False
    return classify_name(s.get("name")) == "person"


def public_supplier(s: dict) -> dict:
    is_person = looks_like_person(s)
    if is_person:
        return {"id": s.get("id"), "name": WITHHELD, "scheme": None, "identifier": None, "country": s.get("country")}
    return {k: s.get(k) for k in ("id", "name", "scheme", "identifier", "country")}


class Procurement:
    def __init__(self, rt: Runtime):
        self.rt = rt
        d = rt.settings.data_dir("procurement")
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(d / "procurement.sqlite"), check_same_thread=False)
        self.conn.executescript(
            "CREATE TABLE IF NOT EXISTS awards (ocid TEXT, award_id TEXT, data TEXT, source TEXT, evidence_id TEXT,"
            " PRIMARY KEY (ocid, award_id));"
            "CREATE TABLE IF NOT EXISTS supplier_checks (skey TEXT PRIMARY KEY, checked_at TEXT, data TEXT);"
            "CREATE TABLE IF NOT EXISTS releases (ocid TEXT, release_id TEXT, data TEXT, PRIMARY KEY (ocid, release_id));"
        )
        self.conn.commit()
        self.registries = Registries(rt.fetcher, rt.settings.companies_house_api_key)
        self.sanctions = Sanctions(rt.fetcher, rt.settings.opensanctions_url, rt.settings.opensanctions_api_key,
                                   d / "sanctions.sqlite")
        self.params = RuleParams()

    # ---------------------------------------------------------------- loading
    def _store_releases(self, releases: list[dict], source: str, evidence_id: str | None) -> dict[str, Any]:
        with self._lock:
            for r in releases:
                self.conn.execute("INSERT OR REPLACE INTO releases VALUES (?,?,?)",
                                  (str(r.get("ocid")), str(r.get("id")), json.dumps(r)))
            self.conn.commit()
        ocids = {str(r.get("ocid")) for r in releases if r.get("ocid")}
        # recompile affected ocids from all stored releases
        all_rel = []
        for ocid in ocids:
            all_rel += [json.loads(x[0]) for x in self.conn.execute("SELECT data FROM releases WHERE ocid=?", (ocid,))]
        awards = compile_awards(all_rel, source)
        with self._lock:
            for a in awards:
                self.conn.execute("INSERT OR REPLACE INTO awards VALUES (?,?,?,?,?)",
                                  (a.ocid, a.award_id, json.dumps(asdict(a)), source, evidence_id))
            self.conn.commit()
        return {"releases": len(releases), "processes": len(ocids), "awards": len(awards)}

    async def load_ocds(self, path_or_url: str) -> dict[str, Any]:
        if path_or_url.startswith(("http://", "https://")):
            res = await self.rt.fetcher.get(path_or_url, max_bytes=200 * 1024 * 1024)
            if res.status != 200:
                raise FetchError(f"HTTP {res.status}", url=path_or_url, status=res.status)
            ev = self.rt.evidence.record(res, note="OCDS data", keep_body=False)
            payload = parse_json_text(res.text())
            src = path_or_url
        else:
            p = self.rt.settings.check_file(path_or_url, (".json", ".jsonl", ".ndjson"))
            if not p.is_file():
                raise ToolInputError(f"file not found: {path_or_url}")
            raw = p.read_bytes()
            ev = self.rt.evidence.record_derived(f"file://{p.resolve()}", note="OCDS file", payload=raw)
            payload = parse_json_text(raw.decode("utf-8-sig", errors="replace"))
            src = p.name
        releases = list(iter_releases(payload))
        if not releases:
            raise ToolInputError("no OCDS releases found (expected a release package, record package or releases)")
        stats = self._store_releases(releases, src, ev.id)
        return ok({**stats, "total_awards_loaded": self._count()}, evidence=[ev])

    async def fetch_uk_awards(self, updated_from: str, updated_to: str | None = None, *, max_pages: int = 5) -> dict[str, Any]:
        params: dict[str, Any] = {"stages": "award", "limit": 100, "updatedFrom": updated_from}
        if updated_to:
            params["updatedTo"] = updated_to
        releases: list[dict] = []
        evidence = []
        cursor = None
        pages = 0
        for _ in range(max(1, min(max_pages, 50))):
            q = dict(params)
            if cursor:
                q["cursor"] = cursor
            res = await self.rt.fetcher.get(FTS_URL, params=q, cache_ttl=3600, max_bytes=64 * 1024 * 1024)
            if res.status != 200:
                raise FetchError(f"Find a Tender HTTP {res.status}", url=res.url, status=res.status)
            evidence.append(self.rt.evidence.record(res, note="Find a Tender OCDS release package", keep_body=False))
            payload = res.json()
            batch = list(iter_releases(payload))
            releases += batch
            pages += 1
            cursor = payload.get("cursor") or _cursor_from_next((payload.get("links") or {}).get("next"))
            if not cursor or not batch:
                break
        stats = self._store_releases(releases, "find-tender.service.gov.uk", evidence[0].id if evidence else None)
        return ok({**stats, "pages": pages, "more_available": bool(cursor), "total_awards_loaded": self._count()},
                  evidence=evidence[:3])

    def load_sanctions_file(self, path: str) -> dict[str, Any]:
        return ok(self.sanctions.load_local(str(self.rt.settings.check_file(path, (".csv",)))))

    def _count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM awards").fetchone()[0]

    def _awards(self) -> list[AwardRecord]:
        rows = self.conn.execute("SELECT data FROM awards").fetchall()
        return [AwardRecord(**json.loads(r[0])) for r in rows]

    # ---------------------------------------------------------------- flags
    def _supplier_check_flags(self, a: AwardRecord) -> list[dict[str, Any]]:
        flags = []
        for s in a.suppliers:
            row = self.conn.execute("SELECT data FROM supplier_checks WHERE skey=?", (supplier_key(s),)).fetchone()
            if not row:
                continue
            chk = json.loads(row[0])
            for m in chk.get("sanctions", []):
                if (m.get("score") or 0) >= 0.9:
                    flags.append({"rule": "sanctions_match", "detail": f"{m.get('name')} ({m.get('datasets')}) "
                                                                       f"score {m.get('score')} — {m.get('url')}"})
            created = _dt(chk.get("incorporated"))
            awarded = _dt(a.award_date)
            if created and awarded and 0 <= (awarded - created).days < self.params.new_supplier_days:
                flags.append({"rule": "new_supplier", "detail": f"incorporated {chk['incorporated']} "
                                                                f"({(awarded - created).days} days before award; "
                                                                f"source {chk.get('registry')})"})
        return flags

    def _all_flags(self) -> tuple[list[AwardRecord], dict[tuple[str, str], list[dict]]]:
        awards = self._awards()
        agg = aggregate_flags(awards, self.params)
        flags = {}
        for a in awards:
            f = award_flags(a, self.params) + agg.get((a.ocid, a.award_id), []) + self._supplier_check_flags(a)
            flags[(a.ocid, a.award_id)] = decorate(f)
        return awards, flags

    def _public_award(self, a: AwardRecord, flags: list[dict]) -> dict[str, Any]:
        return {"ocid": a.ocid, "award_id": a.award_id, "buyer": {"id": a.buyer_id, "name": a.buyer_name},
                "title": a.title, "procedure": a.method, "tenderers": a.n_tenderers, "award_date": a.award_date,
                "award_value": a.award_value, "estimated_value": a.tender_value, "currency": a.currency,
                "suppliers": [public_supplier(s) for s in a.suppliers], "flags": flags,
                "max_severity": flags[0]["severity"] if flags else None, "source": a.source}

    def red_flags(self, *, buyer: str | None = None, supplier: str | None = None, rule: str | None = None,
                  min_severity: str = "low", limit: int = 50) -> dict[str, Any]:
        if not self._count():
            raise ToolInputError("no procurement data loaded", hint="Use load_ocds or fetch_uk_awards first.")
        if rule and rule not in RULES:
            raise ToolInputError(f"unknown rule {rule}; see ruleset")
        min_sev = SEVERITY_ORDER.get(min_severity, 1)
        awards, flags = self._all_flags()
        out = []
        rule_counts: Counter[str] = Counter()
        for a in awards:
            f = [x for x in flags[(a.ocid, a.award_id)] if SEVERITY_ORDER[x["severity"]] >= min_sev]
            if rule:
                f = [x for x in f if x["rule"] == rule]
            if not f:
                continue
            if buyer and buyer.lower() not in f"{a.buyer_id} {a.buyer_name}".lower():
                continue
            if supplier and not any(supplier.lower() in f"{s.get('id')} {s.get('name')} {s.get('identifier')}".lower()
                                    for s in a.suppliers):
                continue
            for x in f:
                rule_counts[x["rule"]] += 1
            out.append(self._public_award(a, f))
        out.sort(key=lambda r: (-SEVERITY_ORDER[r["max_severity"]], -len(r["flags"]), -(r["award_value"] or 0)))
        return ok({"awards_loaded": len(awards), "awards_flagged": len(out), "flags_by_rule": dict(rule_counts),
                   "results": out[:limit]}, limitations=LIMITATIONS)

    def buyer_profile(self, buyer: str) -> dict[str, Any]:
        awards, flags = self._all_flags()
        mine = [a for a in awards if buyer.lower() in f"{a.buyer_id} {a.buyer_name}".lower()]
        if not mine:
            raise ToolInputError(f"no awards for buyer matching {buyer!r}")
        total = sum(a.award_value or 0 for a in mine)
        methods = Counter((a.method or "unknown") for a in mine)
        competitive = [a for a in mine if (a.method or "").lower() in {"open", "selective"} and a.n_tenderers is not None]
        single = sum(1 for a in competitive if a.n_tenderers == 1)
        sup_val: Counter[str] = Counter()
        sup_n: Counter[str] = Counter()
        names: dict[str, dict] = {}
        for a in mine:
            for s in a.suppliers:
                k = supplier_key(s)
                sup_val[k] += (a.award_value or 0) / max(1, len(a.suppliers))
                sup_n[k] += 1
                names[k] = public_supplier(s)
        top = [{"supplier": names[k], "awards": sup_n[k], "value": round(v, 2),
                "share_of_value": round(v / total, 3) if total else None} for k, v in sup_val.most_common(10)]
        hhi = round(sum((v / total) ** 2 for v in sup_val.values()), 4) if total else None
        fl: Counter[str] = Counter(x["rule"] for a in mine for x in flags[(a.ocid, a.award_id)])
        return ok({"buyer": {"id": mine[0].buyer_id, "name": mine[0].buyer_name}, "awards": len(mine),
                   "total_awarded_value": round(total, 2), "currency": Counter(a.currency for a in mine).most_common(1)[0][0],
                   "procedures": dict(methods),
                   "single_bid_rate_competitive": round(single / len(competitive), 3) if competitive else None,
                   "supplier_hhi": hhi, "top_suppliers": top, "flags_by_rule": dict(fl)},
                  limitations=LIMITATIONS + ["HHI = Herfindahl-Hirschman index of supplier value shares (1 = one supplier)."])

    def supplier_profile(self, supplier: str) -> dict[str, Any]:
        awards, flags = self._all_flags()
        mine = [a for a in awards if any(supplier.lower() in f"{s.get('id')} {s.get('name')} {s.get('identifier')}".lower()
                                         for s in a.suppliers)]
        if not mine:
            raise ToolInputError(f"no awards for supplier matching {supplier!r}")
        sup = next(public_supplier(s) for a in mine for s in a.suppliers
                   if supplier.lower() in f"{s.get('id')} {s.get('name')} {s.get('identifier')}".lower())
        buyers = Counter(a.buyer_name for a in mine)
        chk = None
        for a in mine:
            for s in a.suppliers:
                row = self.conn.execute("SELECT data FROM supplier_checks WHERE skey=?", (supplier_key(s),)).fetchone()
                if row:
                    chk = json.loads(row[0])
        return ok({"supplier": sup, "awards": len(mine), "total_value": round(sum(a.award_value or 0 for a in mine), 2),
                   "buyers": buyers.most_common(20),
                   "flagged_awards": [self._public_award(a, flags[(a.ocid, a.award_id)]) for a in mine
                                      if flags[(a.ocid, a.award_id)]][:20],
                   "registry_and_sanctions_check": chk},
                  limitations=LIMITATIONS)

    # ---------------------------------------------------------------- checks
    async def check_supplier(self, name: str, *, country: str | None = None, company_number: str | None = None) -> dict[str, Any]:
        if classify_name(name) == "person":
            raise ToolInputError("this looks like a natural person's name; the tool only checks organisations")
        evidence = []
        warnings = []
        registry: list[dict] = []
        reg_src = None
        incorporated = None
        if company_number or (country or "").upper() in {"GB", "UK"}:
            try:
                ch, res = await self.registries.companies_house(number=company_number, name=None if company_number else name)
                if res is not None:
                    evidence.append(self.rt.evidence.record(res, note="Companies House", keep_body=False))
                    registry = ch
                    reg_src = "companies_house"
                    best = ch[0] if company_number else _best(name, ch, "name")
                    incorporated = best.get("date_of_creation") if best else None
                else:
                    warnings.append("Companies House not checked (COMPANIES_HOUSE_API_KEY not set).")
            except FetchError as exc:
                warnings.append(f"Companies House unavailable: {exc}")
        if not registry:
            try:
                g, res = await self.registries.gleif_by_name(name)
                evidence.append(self.rt.evidence.record(res, note="GLEIF LEI search", keep_body=False))
                registry = g
                reg_src = "gleif"
                best = _best(name, g, "legal_name")
                if best:
                    incorporated = best.get("creation_date")
                    if not incorporated:
                        warnings.append("GLEIF gives LEI registration date, not incorporation date; "
                                        "new_supplier rule not applied from GLEIF alone.")
            except FetchError as exc:
                warnings.append(f"GLEIF unavailable: {exc}")
        sanctions: list[dict] = []
        if self.sanctions.remote_enabled:
            try:
                sanctions, res = await self.sanctions.match_remote(name, country)
                evidence.append(self.rt.evidence.record(res, note="OpenSanctions match", keep_body=False))
            except FetchError as exc:
                warnings.append(f"OpenSanctions API unavailable: {exc}")
        if self.sanctions.local_count():
            sanctions += self.sanctions.match_local(name, country)
        if not self.sanctions.remote_enabled and not self.sanctions.local_count():
            warnings.append("No sanctions source configured (OPENSANCTIONS_API_KEY, a self-hosted yente via "
                            "OPENSANCTIONS_URL, or load_sanctions_file).")
        data = {"name": name, "registry": reg_src, "registry_matches": registry[:5], "incorporated": incorporated,
                "sanctions": sanctions, "checked_at": utcnow_iso()}
        return ok(data, evidence=evidence, warnings=warnings,
                  limitations=["Name matches must be verified against identifiers before acting on them."])

    async def enrich_suppliers(self, *, max_suppliers: int = 20) -> dict[str, Any]:
        awards = self._awards()
        todo: dict[str, dict] = {}
        for a in awards:
            for s in a.suppliers:
                k = supplier_key(s)
                if k in todo or not s.get("name") or looks_like_person(s):
                    continue
                if self.conn.execute("SELECT 1 FROM supplier_checks WHERE skey=?", (k,)).fetchone():
                    continue
                todo[k] = s
        done = []
        for k, s in list(todo.items())[:max_suppliers]:
            number = s.get("identifier") if (s.get("scheme") or "").upper() in {"GB-COH", "GB-CH"} else None
            country = "GB" if number else None
            res = await self.check_supplier(s["name"], country=country, company_number=number)
            if res.get("ok"):
                d = res["data"]
                with self._lock:
                    self.conn.execute("INSERT OR REPLACE INTO supplier_checks VALUES (?,?,?)",
                                      (k, d["checked_at"], json.dumps(d)))
                    self.conn.commit()
                done.append({"supplier": public_supplier(s), "incorporated": d["incorporated"],
                             "sanctions_hits": len(d["sanctions"])})
        return ok({"checked": len(done), "remaining": max(0, len(todo) - len(done)), "results": done})

    def ruleset(self) -> dict[str, Any]:
        return ok({"rules": RULES, "parameters": asdict(self.params)}, limitations=LIMITATIONS)

    def set_params(self, **kwargs: Any) -> dict[str, Any]:
        for k, v in kwargs.items():
            if v is not None:
                if not hasattr(self.params, k):
                    raise ToolInputError(f"unknown parameter {k}")
                setattr(self.params, k, v)
        return self.ruleset()


def _best(name: str, items: list[dict], key: str) -> dict | None:
    from rapidfuzz import fuzz

    target = norm_company(name)
    scored = [(fuzz.token_sort_ratio(target, norm_company(i.get(key) or "")), i) for i in items]
    scored = [s for s in scored if s[0] >= 90]
    return max(scored, key=lambda s: s[0])[1] if scored else None


def _cursor_from_next(next_url: str | None) -> str | None:
    if not next_url or "cursor=" not in next_url:
        return None
    from urllib.parse import parse_qs, urlsplit

    return (parse_qs(urlsplit(next_url).query).get("cursor") or [None])[0]

