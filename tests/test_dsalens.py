"""DSA-Lens tests: local dump ingestion (nested zips), Research API aggregates/count/search, Meta Ad Library."""

from __future__ import annotations

import csv
import io
import json
import random
import zipfile

import pytest

from agent_osint.dsalens.adlibrary import BASE_FIELDS, DSA_FIELDS
from agent_osint.dsalens.service import DSALens
from agent_osint.dsalens.tdb import API
from tests.conftest import dumps

COLS = ["uuid", "decision_visibility", "decision_monetary", "decision_provision", "decision_account", "account_type",
        "decision_ground", "category", "content_type", "automated_detection", "automated_decision", "source_type",
        "territorial_scope", "content_language", "content_date", "application_date", "platform_name", "created_at"]


def _rows(platform: str, n: int, auto_share: float, day: str, seed: int) -> list[dict]:
    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        auto = i < int(n * auto_share)
        rows.append({
            "uuid": f"{platform[:3].lower()}-{day}-{i}",
            "decision_visibility": json.dumps(["DECISION_VISIBILITY_CONTENT_REMOVED"] +
                                              (["DECISION_VISIBILITY_CONTENT_DEMOTED"] if i % 5 == 0 else [])),
            "decision_monetary": "", "decision_provision": "", "decision_account": "", "account_type": "",
            "decision_ground": "DECISION_GROUND_ILLEGAL_CONTENT" if i % 4 == 0 else "DECISION_GROUND_INCOMPATIBLE_CONTENT",
            "category": rnd.choice(["STATEMENT_CATEGORY_SCAMS_AND_FRAUD", "STATEMENT_CATEGORY_ILLEGAL_OR_HARMFUL_SPEECH"])
            if i % 2 else "STATEMENT_CATEGORY_SCAMS_AND_FRAUD",
            "content_type": json.dumps(["CONTENT_TYPE_TEXT"]),
            "automated_detection": "Yes" if auto else "No",
            "automated_decision": "AUTOMATED_DECISION_FULLY" if auto else "AUTOMATED_DECISION_NOT_AUTOMATED",
            "source_type": "SOURCE_VOLUNTARY",
            "territorial_scope": json.dumps(["DE", "FR"] if i % 3 else ["RO"]),
            "content_language": "EN", "content_date": day, "application_date": day, "platform_name": platform,
            "created_at": f"{day} 10:00:00",
        })
    return rows


def _csv_bytes(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLS)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode()


@pytest.fixture
def lens(replay):
    return DSALens(replay.runtime("dsalens"))


def test_local_ingest_nested_zip_and_stats(lens, tmp_path):
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("sor-global-2026-03-01-light-00000.csv", _csv_bytes(_rows("TikTok", 80, 0.9, "2026-03-01", 1)))
        zf.writestr("sor-global-2026-03-01-light-00001.csv", _csv_bytes(_rows("Pinterest", 20, 0.1, "2026-03-01", 2)))
        zf.writestr("readme.txt", "not a csv")
    outer = tmp_path / "sor-global-2026-03-01-light.zip"
    with zipfile.ZipFile(outer, "w") as zf:
        zf.writestr("sor-global-2026-03-01-light.csv.zip", inner.getvalue())
        zf.writestr("unrelated.csv", "a,b\n1,2\n")
    res = lens.ingest_dump(str(outer))
    assert res["data"]["rows_loaded"] == 100 and res["data"]["csv_files"] == 3


async def test_local_queries(lens, tmp_path):
    p = tmp_path / "day.csv"
    p.write_bytes(_csv_bytes(_rows("TikTok", 80, 0.9, "2026-03-01", 1) + _rows("Pinterest", 20, 0.1, "2026-03-02", 2)))
    lens.ingest_dump(str(p))
    by_ground = await lens.moderation_stats(group_by="decision_ground", date_from="2026-03-01", date_to="2026-03-31",
                                            source="local")
    groups = {g["value"]: g["count"] for g in by_ground["data"]["groups"]}
    assert groups == {"DECISION_GROUND_INCOMPATIBLE_CONTENT": 75, "DECISION_GROUND_ILLEGAL_CONTENT": 25}
    ro = await lens.moderation_stats(group_by="platform_name", date_from="2026-03-01", date_to="2026-03-31",
                                     country="RO", source="local")
    assert ro["data"]["total_statements"] == 27 + 7
    vis = await lens.moderation_stats(group_by="decision_visibility", date_from="2026-03-01", date_to="2026-03-31",
                                      platform="tiktok", source="local")
    assert {g["value"]: g["count"] for g in vis["data"]["groups"]} == {
        "DECISION_VISIBILITY_CONTENT_REMOVED": 80, "DECISION_VISIBILITY_CONTENT_DEMOTED": 16}
    cmp_ = await lens.compare_platforms(date_from="2026-03-01", date_to="2026-03-31", source="local")
    rows = {r["platform"]: r for r in cmp_["data"]["platforms"]}
    assert rows["TikTok"]["fully_automated_decision_share"] == 0.9
    assert rows["Pinterest"]["automated_detection_share"] == 0.1
    sample = await lens.sample_statements(n=5, platform="Pinterest", source="local")
    assert len(sample["data"]["statements"]) == 5
    assert all(s["platform_name"] == "Pinterest" for s in sample["data"]["statements"])


async def test_api_aggregates_count_search(replay):
    h = {"Authorization": "Bearer T"}
    for day, (tt_full, tt_none) in {"2026-06-01": (900, 100), "2026-06-02": (800, 200)}.items():
        replay.add(f"{API}/aggregates/{day}/platform_id__automated_decision", {
            "aggregates": [
                {"platform_id": 22, "platform_name": "TikTok", "automated_decision": "AUTOMATED_DECISION_FULLY",
                 "permutation": "x", "total": tt_full},
                {"platform_id": 22, "platform_name": "TikTok", "automated_decision": "AUTOMATED_DECISION_NOT_AUTOMATED",
                 "total": tt_none},
                {"platform_id": 23, "platform_name": "App Store", "automated_decision": "AUTOMATED_DECISION_FULLY",
                 "total": 5}],
            "total": tt_full + tt_none + 5, "date": day}, headers=h)
    lens = DSALens(replay.runtime("dsalens", dsa_tdb_token="T"))
    res = await lens.moderation_stats(group_by="automated_decision", date_from="2026-06-01", date_to="2026-06-02",
                                      platform="TikTok")
    assert res["data"]["source"] == "api"
    assert {g["value"]: g["count"] for g in res["data"]["groups"]} == {
        "AUTOMATED_DECISION_FULLY": 1700, "AUTOMATED_DECISION_NOT_AUTOMATED": 300}
    with pytest.raises(ValueError):
        await lens.moderation_stats(group_by="automated_decision", date_from="2026-06-01", date_to="2026-06-02",
                                    country="DE")
    q = {"bool": {"must": [{"term": {"platform_id": 22}}, {"term": {"category": "STATEMENT_CATEGORY_SCAMS_AND_FRAUD"}}],
                  "filter": [{"range": {"received_date": {"gte": "2026-06-01", "lte": "2026-06-30"}}}]}}
    replay.add(f"{API}/count", method="POST", json_body={"query": q},
               body={"status": "success", "data": {"count": 177303, "_shards": {}}})
    cnt = await lens.count_statements(date_from="2026-06-01", date_to="2026-06-30", platform_id=22,
                                      filters={"category": "STATEMENT_CATEGORY_SCAMS_AND_FRAUD"})
    assert cnt["data"]["count"] == 177303
    fields = ["uuid", "platform_name", "received_date", "category", "decision_ground", "decision_visibility",
              "automated_detection", "automated_decision", "content_type", "territorial_scope", "source_type"]
    replay.add(f"{API}/search", method="POST",
               json_body={"query": {"bool": {"must": [{"term": {"platform_id": 22}}]}}, "size": 2, "_source": fields},
               body={"status": "success", "data": {"hits": {"hits": [{"_source": {"uuid": "u1"}}, {"_source": {"uuid": "u2"}}]}}})
    s = await lens.sample_statements(n=2, platform_id=22)
    assert [x["uuid"] for x in s["data"]["statements"]] == ["u1", "u2"]


async def test_tool_wrapper_returns_structured_errors(replay):
    from agent_osint.dsalens import server

    server._service = None
    res = await server.moderation_stats(group_by="category", date_from="2026-06-01", date_to="2026-06-02")
    assert res["ok"] is False and res["error"]["code"] == "invalid_input"
    assert "ingest_dump" in res["error"]["hint"]
    res = await server.count_statements(date_from="2026-06-01")
    assert res["error"]["code"] == "missing_credential" and "DSA_TDB_TOKEN" in res["error"]["message"]
    server._service = None


async def test_ad_library_with_field_fallback(replay):
    base = "https://graph.facebook.com/v26.0/ads_archive"
    common = ("ad_reached_countries=%5B%22DE%22%5D&ad_type=ALL&ad_active_status=ALL&limit=50&access_token=TOK"
              "&search_terms=heat+pump")
    replay.add(f"{base}?{common}&fields={','.join(BASE_FIELDS + DSA_FIELDS)}",
               {"error": {"message": "(#100) Tried accessing nonexisting field (eu_total_reach)", "code": 100}},
               status=400)
    replay.add(f"{base}?{common}&fields={','.join(BASE_FIELDS)}", {"data": [
        {"id": "1", "page_id": "p1", "page_name": "Green Energy GmbH", "bylines": "Max Mustermann",
         "ad_snapshot_url": "https://www.facebook.com/ads/archive/render_ad/?id=1&access_token=TOK",
         "publisher_platforms": ["facebook", "instagram"], "languages": ["de"],
         "ad_delivery_start_time": "2026-05-01"}], "paging": {}})
    lens = DSALens(replay.runtime("dsalens", meta_ad_library_token="TOK"))
    res = await lens.ad_search(countries=["de"], search_terms="heat pump")
    ad = res["data"]["ads"][0]
    assert ad["page_name"] == "Green Energy GmbH"
    assert "access_token" not in ad["ad_snapshot_url"]
    assert "Mustermann" not in dumps(res)
    assert any("retried with base fields" in w for w in res["warnings"])
