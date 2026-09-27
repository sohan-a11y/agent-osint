"""Procurement tests: official OCDS example releases (Apache-2.0, Open Contracting Partnership) + a synthetic buyer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_osint.procurement.service import FTS_URL, Procurement
from tests.conftest import FIXTURES, dumps

CITY = {"id": "GB-LAC-X1", "name": "City of Example Council"}


def _release(n: int, *, method="open", tenderers=3, tender_days=30, est=100000.0, value=90000.0,
             supplier=None, date="2026-01-10T00:00:00Z", addr=None) -> dict:
    supplier = supplier or {"id": f"GB-COH-{n:08d}", "name": f"Supplier {n} Ltd"}
    party = {"id": supplier["id"], "name": supplier["name"], "roles": ["supplier"],
             "identifier": {"scheme": "GB-COH", "id": supplier["id"].split("-")[-1]} if supplier["id"].startswith("GB-COH") else {},
             "address": addr or {"streetAddress": f"{n} High Street", "postalCode": f"EX{n} 1AA", "countryName": "United Kingdom"}}
    start = "2025-11-01T00:00:00Z"
    end = f"2025-11-{1 + tender_days:02d}T00:00:00Z" if tender_days < 30 else "2025-12-01T00:00:00Z"
    return {"ocid": f"ocds-test-{n:04d}", "id": f"r{n}", "date": date, "tag": ["award"], "buyer": CITY,
            "parties": [{**CITY, "roles": ["buyer"]}, party],
            "tender": {"id": f"t{n}", "title": f"Contract {n}", "procurementMethod": method,
                       "numberOfTenderers": tenderers, "tenderPeriod": {"startDate": start, "endDate": end},
                       "value": {"amount": est, "currency": "GBP"}},
            "awards": [{"id": f"a{n}", "date": date, "value": {"amount": value, "currency": "GBP"},
                        "suppliers": [{"id": supplier["id"], "name": supplier["name"]}]}]}


def _dataset() -> dict:
    big = {"id": "GB-COH-11111111", "name": "Dominant Services Ltd"}
    shared = {"streetAddress": "1 Shell Court", "postalCode": "EX9 9ZZ", "countryName": "United Kingdom"}
    rels = [
        _release(1, supplier=big, value=500000, est=510000, date="2026-01-05T00:00:00Z"),
        _release(2, supplier=big, value=400000, est=400000, date="2026-02-05T00:00:00Z", tenderers=1),
        _release(3, supplier=big, value=300000, est=350000, date="2026-03-05T00:00:00Z", method="limited", tenderers=None),
        _release(4, value=20000, est=30000, tender_days=7),
        _release(5, supplier={"id": "GB-COH-22222222", "name": "Alpha Build Ltd"}, addr=shared, value=10000, est=50000),
        _release(6, supplier={"id": "GB-COH-33333333", "name": "Beta Build Ltd"}, addr=shared, value=12000, est=50000),
        _release(7, supplier={"id": "ST-1", "name": "John Smith"}, value=5000, est=6000, method="direct", tenderers=None),
        _release(8, supplier={"id": "GB-COH-44444444", "name": "Evil Corp Ltd"}, value=15000, est=20000),
        _release(9, supplier={"id": "GB-COH-55555555", "name": "Fresh Start Trading Ltd"}, value=8000, est=9000,
                 date="2026-01-20T00:00:00Z"),
        _release(10, supplier={"id": "GB-COH-55555555", "name": "Fresh Start Trading Ltd"}, value=8500, est=9000,
                 date="2026-02-01T00:00:00Z"),
        _release(11, supplier={"id": "GB-COH-55555555", "name": "Fresh Start Trading Ltd"}, value=8900, est=9000,
                 date="2026-02-15T00:00:00Z"),
    ]
    return {"uri": "test", "publishedDate": "2026-03-10T00:00:00Z", "releases": rels}


@pytest.fixture
def proc(replay):
    return Procurement(replay.runtime("procurement"))


async def test_official_ocds_change_history_example(proc):
    files = sorted((FIXTURES / "ocds_change_history").glob("*.json"))
    for f in files:
        await proc.load_ocds(str(f))
    res = proc.red_flags()["data"]
    assert res["awards_loaded"] == 1
    award = res["results"][0]
    assert award["buyer"]["name"] == "London Borough of Barnet"
    assert award["suppliers"][0]["name"] == "Balfour Beatty"
    rules = {f["rule"] for f in award["flags"]}
    # The example's award (11,000,000) exceeds its estimate (1,100,000); contract later amended to 13,500,000.
    assert {"award_well_above_estimate", "contract_value_increase"} <= rules


async def test_rules_profiles_and_privacy(proc, tmp_path: Path):
    p = tmp_path / "pkg.json"
    p.write_text(json.dumps(_dataset()))
    await proc.load_ocds(str(p))
    proc.set_params(split_threshold=10000)
    res = proc.red_flags(limit=100)["data"]
    counts = res["flags_by_rule"]
    assert counts["supplier_concentration"] == 3
    assert counts["single_bidder"] == 1
    assert counts["non_open_procedure"] == 2
    assert counts["short_tender_period"] == 1
    assert counts["shared_address_suppliers"] == 2
    assert counts["possible_split_purchases"] == 3
    assert counts["award_at_or_above_estimate"] == 3  # 500k/510k, 400k/400k, 8.9k/9k
    buyer = proc.buyer_profile("City of Example")["data"]
    assert buyer["awards"] == 11
    assert buyer["top_suppliers"][0]["supplier"]["name"] == "Dominant Services Ltd"
    assert buyer["single_bid_rate_competitive"] == round(1 / 9, 3)  # 9 open procedures, 1 single bid
    blob = dumps([res, buyer])
    assert "John Smith" not in blob and "[individual — withheld]" in blob
    sup = proc.supplier_profile("Dominant")["data"]
    assert sup["awards"] == 3


async def test_supplier_checks_sanctions_and_new_company(proc, replay, tmp_path: Path):
    p = tmp_path / "pkg.json"
    p.write_text(json.dumps(_dataset()))
    await proc.load_ocds(str(p))
    sanc = tmp_path / "targets.simple.csv"
    sanc.write_text("id,schema,name,aliases,birth_date,countries,addresses,identifiers,sanctions,phones,emails,dataset,"
                    "first_seen,last_seen,last_change\n"
                    "NK-evil,Company,EVIL CORP LIMITED,Evil Corporation,,gb,,,UK sanctions list,,,gb_hmt_sanctions,,,\n"
                    "NK-p,Person,John Smith,,1970-01-01,gb,,,x,,,y,,,\n")
    assert proc.load_sanctions_file(str(sanc))["data"]["entity_names_loaded"] == 2
    replay.add("https://api.company-information.service.gov.uk/company/55555555",
               {"company_number": "55555555", "company_name": "FRESH START TRADING LTD", "company_status": "active",
                "date_of_creation": "2025-12-01", "type": "ltd"})
    for num in ("11111111", "22222222", "33333333", "44444444", "00000004"):
        replay.add(f"https://api.company-information.service.gov.uk/company/{num}",
                   {"company_number": num, "company_name": "X", "date_of_creation": "1999-01-01"})
    proc.registries.ch_key = "CHKEY"
    res = await proc.enrich_suppliers(max_suppliers=50)
    assert res["data"]["checked"] == 6  # the sole trader is excluded; 6 companies
    flags = proc.red_flags(min_severity="high")["data"]
    rules = flags["flags_by_rule"]
    assert rules["sanctions_match"] == 1
    assert rules["new_supplier"] == 3
    one = await proc.check_supplier("Evil Corp Ltd")
    assert one["data"]["sanctions"][0]["id"] == "NK-evil"
    with pytest.raises(ValueError):
        await proc.check_supplier("John Smith")


async def test_fetch_uk_awards_cursor(proc, replay):
    rel1 = _release(21)
    rel2 = _release(22)
    q = "limit=100&stages=award&updatedFrom=2026-09-01T00:00:00"
    replay.add(f"{FTS_URL}?{q}", {"releases": [rel1], "cursor": "ABC="})
    replay.add(f"{FTS_URL}?{q}&cursor=ABC=", {"releases": [rel2], "links": {}})
    res = await proc.fetch_uk_awards("2026-09-01T00:00:00")
    assert res["data"]["pages"] == 2 and res["data"]["awards"] == 2
