"""CommentForensics tests: API paging incl. the >5,000 lastModifiedDate window, mirror, CSV, clustering."""

from __future__ import annotations

import csv

import pytest

from agent_osint.commentforensics.cluster import cluster_comments, display_text, is_attachment_only
from agent_osint.commentforensics.service import CommentForensics
from agent_osint.commentforensics.sources import utc_to_eastern_filter
from tests.conftest import dumps

D = "EPA-HQ-OAR-2099-0001"
OID = "0900006400000001"
API = "https://api.regulations.gov/v4"

TEMPLATE_A = ("I strongly oppose the proposed rule on emissions reporting. It will impose unreasonable costs on "
              "small manufacturers in my community and will not meaningfully improve air quality. The agency "
              "should withdraw this proposal and work with industry on a voluntary approach instead.")
TEMPLATE_B = ("As a member of the Sierra Club, I urge you to finalize the strongest possible standards for methane "
              "emissions from oil and gas facilities. Our climate and our health depend on it. Sent via Action "
              "Network https://actionnetwork.org/letters/methane")


def _comments() -> list[dict]:
    rows = []
    names = ["John Doe", "Mary Major", "Pat Kim", "Alex Rivera", "Sam Lee", "Chris Park", "Jo Brown", "Lee Wong",
             "Ana Silva", "Tom Hardy", "Nina Patel", "Omar Farouk"]
    for i, n in enumerate(names):
        extra = " This matters to my family business." if i % 4 == 0 else ""
        rows.append({"id": f"{D}-{1000 + i}", "text": f"Dear Administrator,\n{TEMPLATE_A}{extra}\nSincerely,\n{n}",
                     "org": None, "date": f"2099-03-0{1 + i % 3}T12:00:00Z"})
    for i in range(6):
        rows.append({"id": f"{D}-{2000 + i}", "text": TEMPLATE_B, "org": None if i else "Sierra Club",
                     "date": "2099-03-05T09:00:00Z"})
    uniques = ["The cost-benefit analysis in section 4 double counts compliance costs for facilities under 10 tons.",
               "Please extend the comment period by 60 days so rural cooperatives can respond.",
               "We support the monitoring provisions but request clarity on data retention requirements for CEMS.",
               "My name is Robert Smith and I think the rule is great. Call me at 555-201-3344.",
               "The definition of 'affected facility' conflicts with the 2015 rule and should be harmonized."]
    for i, t in enumerate(uniques):
        rows.append({"id": f"{D}-{3000 + i}", "text": t, "org": "Acme Chemical Corp" if i == 0 else None,
                     "date": "2099-03-10T10:00:00Z"})
    rows.append({"id": f"{D}-4000", "text": "See attached file(s)", "org": "Rural Electric Cooperative Association",
                 "date": "2099-03-11T10:00:00Z", "attach": True})
    rows.append({"id": f"{D}-4001", "text": "Please see attached comments.", "org": None,
                 "date": "2099-03-11T10:00:00Z", "attach": True})
    return rows


def _detail(row: dict, docket: str = D) -> dict:
    return {"data": {"id": row["id"], "type": "comments", "attributes": {
        "comment": row["text"], "docketId": docket, "commentOnDocumentId": f"{D}-0001", "objectId": "obj" + row["id"][-4:],
        "postedDate": row["date"], "modifyDate": row["date"], "title": f"Comment from {row['id']}",
        "organization": row.get("org"), "firstName": "Secret", "lastName": "Person", "withdrawn": False},
        "relationships": {"attachments": {"data": [{"id": "a1", "type": "attachments"}] if row.get("attach") else []}}}}


def _list_item(row: dict, lmd: str) -> dict:
    return {"id": row["id"], "type": "comments", "attributes": {"objectId": "obj" + row["id"][-4:],
                                                                "lastModifiedDate": lmd, "postedDate": row["date"],
                                                                "title": "t", "withdrawn": False}}


def _serve_api(replay, rows: list[dict]) -> None:
    h = {"X-Api-Key": "k"}
    replay.add(f"{API}/documents?filter[docketId]={D}&page[size]=250&page[number]=1", {
        "data": [{"id": f"{D}-0001", "attributes": {"objectId": OID, "documentType": "Proposed Rule",
                                                     "title": "Emissions Reporting", "commentEndDate": "2099-04-01T03:59:59Z"}}],
        "meta": {"hasNextPage": False, "totalElements": 1}}, headers=h)
    # Pages 1..20 of the first window carry one comment each (enough to exercise the paging logic).
    extra = rows[:20]
    rest = rows[20:]
    for page in range(1, 21):
        item = extra[page - 1]
        lmd = f"2099-03-{10 + page // 10:02d}T15:58:{page:02d}Z"
        replay.add(f"{API}/comments?filter[commentOnId]={OID}&page[size]=250&page[number]={page}"
                   "&sort=lastModifiedDate,documentId",
                   {"data": [_list_item(item, lmd)], "meta": {"hasNextPage": True, "totalElements": len(rows) + 1,
                                                              "pageNumber": page}})
    # Next window: filter[lastModifiedDate][ge] in US Eastern time (15:58:20Z on 2099-03-12 -> 11:58:20 EDT)
    last_lmd = f"2099-03-{10 + 20 // 10:02d}T15:58:20Z"
    since = utc_to_eastern_filter(last_lmd)
    replay.add(f"{API}/comments?filter[commentOnId]={OID}&page[size]=250&page[number]=1"
               f"&sort=lastModifiedDate,documentId&filter[lastModifiedDate][ge]={since}",
               {"data": [_list_item(r, "2099-03-13T00:00:00Z") for r in rest]
                + [_list_item({"id": f"{D}-9999", "date": "2099-03-13T00:00:00Z"}, "2099-03-13T00:00:00Z"),
                   _list_item({"id": "OTHER-2099-0001-0001", "date": "2099-03-13T00:00:00Z"}, "2099-03-13T00:00:00Z")],
                "meta": {"hasNextPage": False}})
    for r in rows:
        replay.add(f"{API}/comments/{r['id']}?include=attachments", _detail(r))
    replay.add(f"{API}/comments/{D}-9999?include=attachments", {"errors": []}, status=404)
    replay.add(f"{API}/comments/OTHER-2099-0001-0001?include=attachments",
               _detail({"id": "OTHER-2099-0001-0001", "text": TEMPLATE_A, "date": "2099-03-01T00:00:00Z"},
                        docket="OTHER-2099-0001"))


def test_eastern_time_conversion_matches_gsa_example():
    # GSA docs: lastModifiedDate 2020-08-10T15:58:52Z "translates to 2020-08-10 11:58:52 in Eastern time"
    assert utc_to_eastern_filter("2020-08-10T15:58:52Z") == "2020-08-10 11:58:52"
    assert utc_to_eastern_filter("2020-01-10T15:58:52Z") == "2020-01-10 10:58:52"  # EST in winter


def test_display_text_strips_salutation_and_signature():
    t = display_text("Dear Administrator,\nThe rule is bad.\nSincerely,\nJohn Doe\nSpringfield")
    assert t == "The rule is bad."
    assert is_attachment_only("See attached file(s)")
    assert not is_attachment_only("See the attached analysis, which shows costs are overstated by a factor of 3.")


def test_clustering_groups_templates_and_personalised_variants():
    rows = [{"comment_id": r["id"], "text": r["text"], "organization": r.get("org"), "posted_date": r["date"]}
            for r in _comments()]
    res = cluster_comments(rows, threshold=0.8, min_size=2)
    sizes = sorted(c["size"] for c in res.clusters)
    assert sizes == [6, 12]
    a = next(c for c in res.clusters if c["size"] == 12)
    assert a["unique_text_variants"] == 2 and a["personalised_share"] == 0.25
    assert res.n_attachment_only == 2


@pytest.fixture
def cf(replay):
    rows = _comments()
    _serve_api(replay, rows)
    rt = replay.runtime("commentforensics", regulations_gov_api_key="k")
    return CommentForensics(rt)


async def test_api_ingest_resumable_with_windowing(cf):
    first = await cf.ingest(D, max_requests=10)
    assert first["data"]["phase"] == "listing" and not first["data"]["complete"]
    second = await cf.ingest(D, max_requests=15)
    assert second["data"]["phase"] == "text"
    final = await cf.ingest(D, max_requests=500)
    data = final["data"]
    assert data["complete"]
    assert data["known_comments"] == len(_comments()) + 2
    assert data["mismatched_records_excluded"] == 1  # the OTHER docket record is excluded
    summary = cf.docket_summary(D)["data"]
    assert summary["campaigns_at_min_size"] == 2
    assert summary["comments_in_campaigns"] == 18
    assert summary["attachment_only"] == 2
    assert ("Acme Chemical Corp", 1) in [tuple(x) for x in summary["organizations_top"]]


async def test_campaigns_detail_sponsors_and_privacy(cf):
    await cf.ingest(D, max_requests=1000)
    res = cf.campaign_clusters(D, min_size=5)
    assert res["data"]["campaigns"] == 2
    clusters = res["data"]["clusters"]
    b = next(c for c in clusters if c["size"] == 6)
    detail = cf.cluster_detail(D, b["cluster_id"], sample=3)["data"]
    assert len(detail["sample_comments"]) == 3
    assert detail["sample_comments"][0]["url"].startswith("https://www.regulations.gov/comment/")
    hints = cf.sponsor_hints(D, b["cluster_id"])["data"]["hints"]
    values = {h["type"]: h["value"] for h in hints}
    assert values["domain_mentioned"] == "actionnetwork.org"
    assert values["submission_platform_marker"] in {"Action Network", "Generic 'sent via' footer"}
    assert values["affiliation_phrase"].startswith("Sierra Club")
    search = cf.search_comments(D, "Robert Smith")
    assert search["data"]["total_matches"] == 1
    blob = dumps([res, detail, hints, search["data"]["matches"], cf.docket_summary(D)])
    for secret in ("Secret", "John Doe", "Omar Farouk", "Robert Smith", "555-201-3344"):
        assert secret not in blob


async def test_mirrulations_source(replay):
    rows = _comments()[:4]
    prefix = f"raw-data/EPA/{D}/text-{D}/comments/"
    def listing(keys, token=None):
        items = "".join(f"<Contents><Key>{prefix}{k}.json</Key></Contents>" for k in keys)
        nxt = f"<NextContinuationToken>{token}</NextContinuationToken>" if token else ""
        return (f'<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"{items}{nxt}</ListBucketResult>")
    base = f"https://mirrulations.s3.amazonaws.com/?list-type=2&prefix={prefix}&max-keys=1000"
    replay.add(base, listing([rows[0]["id"], rows[1]["id"]], token="T1"))
    replay.add(base + "&continuation-token=T1", listing([rows[2]["id"], rows[3]["id"] + "(1)"]))
    for r in rows[:3]:
        replay.add(f"https://mirrulations.s3.amazonaws.com/{prefix}{r['id']}.json", _detail(r))
    replay.add(f"https://mirrulations.s3.amazonaws.com/{prefix}{rows[3]['id']}(1).json", _detail(rows[3]))
    cf = CommentForensics(replay.runtime("commentforensics"))
    res = await cf.ingest(D, source="auto")
    assert res["data"]["source"] == "mirrulations"
    assert res["data"]["complete"] and res["data"]["with_text"] == 4
    ids = {r["comment_id"] for r in cf.store.texts(D)}
    assert f"{rows[3]['id']}(1)" in ids


def test_bulk_csv(replay, tmp_path):
    path = tmp_path / "export.csv"
    cols = ["Document ID", "Agency ID", "Docket ID", "Posted Date", "Comment", "First Name", "Last Name",
            "Organization Name", "Attachment Files"]
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in _comments():
            w.writerow({"Document ID": r["id"], "Agency ID": "EPA", "Docket ID": D, "Posted Date": r["date"],
                        "Comment": r["text"], "First Name": "Secret", "Last Name": "Person",
                        "Organization Name": r.get("org") or "", "Attachment Files": "x.pdf" if r.get("attach") else ""})
        w.writerow({"Document ID": f"{D}-1000", "Docket ID": D, "Comment": TEMPLATE_A, "Posted Date": "2099-03-01"})
    cf = CommentForensics(replay.runtime("commentforensics"))
    res = cf.ingest_csv(str(path))
    assert res["data"]["rows_loaded"] == len(_comments()) + 1  # reused Document ID kept as a distinct row
    summary = cf.docket_summary(D)["data"]
    assert summary["comments_in_campaigns"] == 19
    assert "Secret" not in dumps(summary)


async def test_missing_key_and_bad_docket(replay):
    cf = CommentForensics(replay.runtime("commentforensics"))
    with pytest.raises(Exception) as exc:
        await cf.ingest(D, source="api")
    assert "REGULATIONS_GOV_API_KEY" in str(exc.value)
    with pytest.raises(ValueError):
        cf.campaign_clusters("not a docket")
