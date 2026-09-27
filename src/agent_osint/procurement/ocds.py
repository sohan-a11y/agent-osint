"""Open Contracting Data Standard (OCDS 1.1) loading and flattening to award records.

Fields used (OCDS 1.1 schema): ``ocid``, ``id``, ``date``, ``buyer{id,name}``, ``parties[]{id,name,roles,
identifier{scheme,id}, address{streetAddress,locality,postalCode,countryName}}``,
``tender{id,title,procurementMethod,procurementMethodDetails,numberOfTenderers,tenderPeriod{startDate,endDate},
value{amount,currency},mainProcurementCategory}``, ``awards[]{id,date,status,value,suppliers[]{id,name}}``,
``contracts[]{awardID,value,dateSigned}``, ``bids.statistics[]{measure,value}``.
Releases sharing an ``ocid`` are merged in date order (later values win), which approximates an OCDS
compiled record.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AwardRecord:
    ocid: str
    award_id: str
    buyer_id: str | None
    buyer_name: str | None
    title: str | None
    method: str | None
    method_details: str | None
    category: str | None
    n_tenderers: int | None
    tender_start: str | None
    tender_end: str | None
    tender_value: float | None
    award_value: float | None
    contract_value: float | None
    currency: str | None
    award_date: str | None
    award_status: str | None
    suppliers: list[dict[str, Any]] = field(default_factory=list)
    release_ids: list[str] = field(default_factory=list)
    source: str = ""


def _num(v: Any) -> float | None:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        elif isinstance(v, list) and k in {"parties", "awards", "contracts"} and isinstance(out.get(k), list):
            by_id = {str(x.get("id")): x for x in out[k] if isinstance(x, dict)}
            for x in v:
                if isinstance(x, dict) and x.get("id") is not None and str(x["id"]) in by_id:
                    by_id[str(x["id"])] = _deep_merge(by_id[str(x["id"])], x)
                elif isinstance(x, dict):
                    by_id[str(x.get("id", len(by_id)))] = x
            out[k] = list(by_id.values())
        elif v not in (None, "", [], {}):
            out[k] = v
    return out


def iter_releases(payload: Any) -> Iterable[dict]:
    """Accept a release package, record package, list of releases, or a single release."""
    if isinstance(payload, list):
        for item in payload:
            yield from iter_releases(item)
        return
    if not isinstance(payload, dict):
        return
    if "releases" in payload and isinstance(payload["releases"], list):
        yield from (r for r in payload["releases"] if isinstance(r, dict))
    elif "records" in payload and isinstance(payload["records"], list):
        for rec in payload["records"]:
            if isinstance(rec.get("compiledRelease"), dict):
                yield rec["compiledRelease"]
            else:
                yield from (r for r in rec.get("releases", []) if isinstance(r, dict) and "ocid" in r)
    elif "ocid" in payload:
        yield payload


def parse_json_text(text: str) -> Any:
    text = text.strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return [json.loads(line) for line in text.splitlines() if line.strip()]  # JSON Lines
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON or JSON Lines: {exc}") from None


def compile_awards(releases: Iterable[dict], source: str) -> list[AwardRecord]:
    by_ocid: dict[str, list[dict]] = {}
    for r in releases:
        if r.get("ocid"):
            by_ocid.setdefault(str(r["ocid"]), []).append(r)
    awards: list[AwardRecord] = []
    for ocid, rels in by_ocid.items():
        rels.sort(key=lambda r: str(r.get("date") or ""))
        merged: dict = {}
        for r in rels:
            merged = _deep_merge(merged, r)
        parties = {str(p.get("id")): p for p in merged.get("parties", []) if isinstance(p, dict)}
        tender = merged.get("tender") or {}
        buyer = merged.get("buyer") or {}
        if not buyer:
            buyer = next(({"id": p.get("id"), "name": p.get("name")} for p in parties.values()
                          if "buyer" in (p.get("roles") or [])), {})
        n_tenderers = tender.get("numberOfTenderers")
        if n_tenderers is None:
            for stat in ((merged.get("bids") or {}).get("statistics") or []):
                if stat.get("measure") in ("bids", "validBids") and stat.get("value") is not None:
                    n_tenderers = stat["value"]
                    break
        period = tender.get("tenderPeriod") or {}
        contracts = [c for c in merged.get("contracts", []) if isinstance(c, dict)]
        for aw in merged.get("awards", []) or []:
            if not isinstance(aw, dict):
                continue
            sups = []
            for s in aw.get("suppliers") or []:
                p = parties.get(str(s.get("id"))) or {}
                ident = p.get("identifier") or {}
                addr = p.get("address") or {}
                sups.append({"id": s.get("id"), "name": s.get("name") or p.get("name"),
                             "scheme": ident.get("scheme"), "identifier": ident.get("id"),
                             "legal_name": ident.get("legalName"),
                             "postal_code": addr.get("postalCode"), "street": addr.get("streetAddress"),
                             "country": addr.get("countryName") or addr.get("country")})
            cvals = [_num((c.get("value") or {}).get("amount")) for c in contracts
                     if str(c.get("awardID")) == str(aw.get("id"))]
            cvals = [v for v in cvals if v is not None]
            value = aw.get("value") or {}
            awards.append(AwardRecord(
                ocid=ocid, award_id=str(aw.get("id")), buyer_id=buyer.get("id"), buyer_name=buyer.get("name"),
                title=tender.get("title") or aw.get("title"), method=tender.get("procurementMethod"),
                method_details=tender.get("procurementMethodDetails"), category=tender.get("mainProcurementCategory"),
                n_tenderers=int(n_tenderers) if isinstance(n_tenderers, (int, float, str)) and str(n_tenderers).isdigit() else None,
                tender_start=period.get("startDate"), tender_end=period.get("endDate"),
                tender_value=_num((tender.get("value") or {}).get("amount")),
                award_value=_num(value.get("amount")), contract_value=max(cvals) if cvals else None,
                currency=value.get("currency") or (tender.get("value") or {}).get("currency"),
                award_date=aw.get("date") or merged.get("date"), award_status=aw.get("status"),
                suppliers=sups, release_ids=[str(r.get("id")) for r in rels], source=source,
            ))
    return awards
