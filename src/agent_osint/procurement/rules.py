"""Procurement red-flag rules.

Based on indicators widely used in the procurement-integrity literature (e.g. Open Contracting Partnership,
"Red Flags for Integrity: Giving the green light to open data solutions" (2016); Fazekas & Kocsis, "Uncovering
High-Level Corruption: Cross-National Objective Corruption Risk Indicators Using Public Procurement Data",
British Journal of Political Science 50(1), 2020). Thresholds are parameters, not legal standards; tune them to
the jurisdiction. A red flag is a reason to look closer, never evidence of wrongdoing.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .ocds import AwardRecord

RULES: dict[str, dict[str, Any]] = {
    "single_bidder": {"severity": "high", "desc": "Competitive procedure received only one bid."},
    "non_open_procedure": {"severity": "medium", "desc": "Limited/direct (non-open) procedure used."},
    "short_tender_period": {"severity": "medium", "desc": "Tender period shorter than the threshold."},
    "award_at_or_above_estimate": {"severity": "low", "desc": "Award value within 2% of, or above, the estimate."},
    "award_well_above_estimate": {"severity": "medium", "desc": "Award value >10% above the estimated value."},
    "contract_value_increase": {"severity": "medium", "desc": "Contract value >20% above the award value."},
    "supplier_concentration": {"severity": "medium", "desc": "Supplier won >50% of this buyer's awarded value (≥3 awards)."},
    "shared_address_suppliers": {"severity": "medium", "desc": "Different suppliers of the same buyer share an address."},
    "possible_split_purchases": {"severity": "medium", "desc": "≥3 awards to the same supplier by the same buyer within 60 days, each below the given threshold."},
    "missing_key_data": {"severity": "low", "desc": "Award lacks a value or supplier identifier."},
    "new_supplier": {"severity": "high", "desc": "Supplier incorporated less than N days before the award (registry)."},
    "sanctions_match": {"severity": "critical", "desc": "Supplier name matches a sanctioned/listed entity (verify!)."},
}
SEVERITY_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1}


@dataclass
class RuleParams:
    short_tender_days: int = 20
    concentration_share: float = 0.5
    concentration_min_awards: int = 3
    split_threshold: float | None = None
    new_supplier_days: int = 365


def _dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        try:
            return datetime.strptime(v[:10], "%Y-%m-%d")
        except ValueError:
            return None


def supplier_key(s: dict) -> str:
    if s.get("scheme") and s.get("identifier"):
        return f"{s['scheme']}:{s['identifier']}".upper()
    return f"name:{(s.get('name') or '').strip().lower()}"


def award_flags(a: AwardRecord, p: RuleParams) -> list[dict[str, Any]]:
    flags = []
    method = (a.method or "").lower()
    if a.n_tenderers == 1 and method in {"open", "selective"}:
        flags.append({"rule": "single_bidder", "detail": f"numberOfTenderers=1 under '{a.method}' procedure"})
    if method in {"limited", "direct"}:
        flags.append({"rule": "non_open_procedure", "detail": f"procurementMethod='{a.method}'"
                      + (f" ({a.method_details})" if a.method_details else "")})
    start, end = _dt(a.tender_start), _dt(a.tender_end)
    if start and end and method in {"open", "selective"}:
        days = (end - start).days
        if 0 <= days < p.short_tender_days:
            flags.append({"rule": "short_tender_period", "detail": f"{days} days (< {p.short_tender_days})"})
    if a.award_value and a.tender_value:
        ratio = a.award_value / a.tender_value
        if ratio > 1.10:
            flags.append({"rule": "award_well_above_estimate", "detail": f"award/estimate = {ratio:.2f}"})
        elif ratio >= 0.98:
            flags.append({"rule": "award_at_or_above_estimate", "detail": f"award/estimate = {ratio:.3f}"})
    if a.contract_value and a.award_value and a.contract_value > 1.2 * a.award_value:
        flags.append({"rule": "contract_value_increase",
                      "detail": f"contract {a.contract_value:,.0f} vs award {a.award_value:,.0f}"})
    if a.award_value is None or any(not s.get("identifier") for s in a.suppliers) or not a.suppliers:
        flags.append({"rule": "missing_key_data",
                      "detail": ", ".join(x for x in ["no award value" if a.award_value is None else "",
                                                      "supplier without identifier" if any(not s.get('identifier') for s in a.suppliers) else "",
                                                      "no supplier" if not a.suppliers else ""] if x)})
    return flags


def aggregate_flags(awards: list[AwardRecord], p: RuleParams) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Flags that need the whole dataset. Keyed by (ocid, award_id)."""
    out: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    by_buyer: dict[str, list[AwardRecord]] = defaultdict(list)
    for a in awards:
        by_buyer[a.buyer_id or a.buyer_name or "?"].append(a)
    for _buyer, items in by_buyer.items():
        total = sum(a.award_value or 0 for a in items)
        per_sup: dict[str, list[AwardRecord]] = defaultdict(list)
        for a in items:
            for s in a.suppliers:
                per_sup[supplier_key(s)].append(a)
        for sawards in per_sup.values():
            val = sum(a.award_value or 0 for a in sawards)
            if total > 0 and len(sawards) >= p.concentration_min_awards and val / total > p.concentration_share:
                for a in sawards:
                    out[(a.ocid, a.award_id)].append({"rule": "supplier_concentration",
                                                      "detail": f"{val / total:.0%} of buyer's awarded value across {len(sawards)} awards"})
            if p.split_threshold:
                dated = sorted((d, a) for a in sawards if (d := _dt(a.award_date)) and a.award_value is not None
                               and a.award_value < p.split_threshold)
                for i in range(len(dated)):
                    window = [x for x in dated if 0 <= (x[0] - dated[i][0]).days <= 60]
                    if len(window) >= 3:
                        for _, a in window:
                            key = (a.ocid, a.award_id)
                            if not any(f["rule"] == "possible_split_purchases" for f in out[key]):
                                out[key].append({"rule": "possible_split_purchases",
                                                 "detail": f"{len(window)} awards < {p.split_threshold:,.0f} within 60 days"})
        # shared addresses among distinct suppliers of the same buyer
        addr_map: dict[str, set[str]] = defaultdict(set)
        addr_awards: dict[str, list[AwardRecord]] = defaultdict(list)
        for a in items:
            for s in a.suppliers:
                if s.get("postal_code") and s.get("street"):
                    addr = re.sub(r"\W+", "", f"{s['postal_code']}{s['street']}".lower())
                    addr_map[addr].add(supplier_key(s))
                    addr_awards[addr].append(a)
        for addr, sups in addr_map.items():
            if len(sups) >= 2:
                for a in addr_awards[addr]:
                    key = (a.ocid, a.award_id)
                    if not any(f["rule"] == "shared_address_suppliers" for f in out[key]):
                        out[key].append({"rule": "shared_address_suppliers",
                                         "detail": f"{len(sups)} distinct suppliers registered at the same address"})
    return out


def decorate(flags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for f in flags:
        rule = RULES[f["rule"]]
        out.append({**f, "severity": rule["severity"], "meaning": rule["desc"]})
    out.sort(key=lambda f: -SEVERITY_ORDER[f["severity"]])
    return out
