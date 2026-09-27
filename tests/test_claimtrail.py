"""ClaimTrail tests. C2PA samples are from contentauth/c2pa-python test fixtures (Apache-2.0 / MIT, Adobe)."""

from __future__ import annotations

import json

import pytest

from agent_osint.claimtrail.service import ClaimTrail, _norm_date
from agent_osint.claimtrail.sources import FACTCHECK_URL, extract_claimreview, extract_page_dates
from agent_osint.core.wayback import CDX_URL
from tests.conftest import FIXTURES, dumps

ARTICLE = """<html><head><title>Viral photo of flooded airport</title>
<meta property="article:published_time" content="2026-08-02T09:15:00+02:00">
<meta property="article:modified_time" content="2026-08-03T10:00:00Z">
<link rel="canonical" href="https://news.example.org/2026/08/flooded-airport">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"NewsArticle","datePublished":"2026-08-02T07:15:00Z"}</script>
</head><body><time datetime="2026-08-02">2 Aug</time></body></html>"""

FACTCHECK_ARTICLE = """<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"ClaimReview","datePublished":"2026-08-04",
 "url":"https://factcheck.example.org/flooded-airport",
 "claimReviewed":"Photo shows Example Airport flooded this week",
 "author":{"@type":"Organization","name":"Example Fact Check"},
 "reviewRating":{"@type":"Rating","ratingValue":1,"alternateName":"False"},
 "itemReviewed":{"@type":"Claim","datePublished":"2026-08-01","author":{"@type":"Person","name":"Jane Poster"},
   "appearance":[{"@type":"CreativeWork","url":"https://social.example.com/p/123"}]}}
</script></head><body></body></html>"""


def test_page_dates_and_claimreview():
    pd = extract_page_dates(ARTICLE, "https://news.example.org/x", "Tue, 04 Aug 2026 10:00:00 GMT")
    kinds = {(d["kind"], d["source"]) for d in pd.dates}
    assert ("published", "meta article:published_time") in kinds and ("published", "json-ld datePublished") in kinds
    assert pd.canonical.endswith("flooded-airport")
    assert _norm_date("2026-08-02T09:15:00+02:00") == "2026-08-02T07:15:00Z"
    assert _norm_date("Tue, 04 Aug 2026 10:00:00 GMT") == "2026-08-04T10:00:00Z"
    cr = extract_claimreview(FACTCHECK_ARTICLE)[0]
    assert cr["rating"] == "False" and cr["fact_checker"] == "Example Fact Check"
    assert cr["claimant"] == "[individual — withheld]"
    assert cr["appearances"] == ["https://social.example.com/p/123"]


@pytest.fixture
def ct(replay):
    url = "https://news.example.org/2026/08/flooded-airport"
    bare = "news.example.org/2026/08/flooded-airport"
    base = f"{CDX_URL}?url={bare}&output=json&fl=timestamp,original,statuscode,digest,mimetype&filter=statuscode:200"
    hdr = ["timestamp", "original", "statuscode", "digest", "mimetype"]
    replay.add(base + "&limit=1", [hdr, ["20260802081000", url, "200", "D1", "text/html"]])
    replay.add(base + "&limit=-1", [hdr, ["20260901000000", url, "200", "D2", "text/html"]])
    replay.add(f"https://web.archive.org/web/20260802081000id_/{url}", ARTICLE, headers={"content-type": "text/html"})
    replay.add("https://news.example.org/robots.txt", status=404)
    replay.add(url, ARTICLE, headers={"content-type": "text/html", "last-modified": "Tue, 04 Aug 2026 10:00:00 GMT"})
    replay.add("https://factcheck.example.org/robots.txt", status=404)
    replay.add("https://factcheck.example.org/flooded-airport", FACTCHECK_ARTICLE, headers={"content-type": "text/html"})
    replay.add(f"{FACTCHECK_URL}?query=flooded+airport+photo&pageSize=20&key=K", {"claims": [
        {"text": "Photo shows Example Airport flooded", "claimant": "Viral image", "claimDate": "2026-08-01T00:00:00Z",
         "claimReview": [{"publisher": {"name": "Example Fact Check", "site": "factcheck.example.org"},
                          "url": "https://factcheck.example.org/flooded-airport", "title": "No, this photo is from 2019",
                          "reviewDate": "2026-08-04T00:00:00Z", "textualRating": "False", "languageCode": "en"}]}]})
    return ClaimTrail(replay.runtime("claimtrail", google_factcheck_api_key="K"))


async def test_url_timeline(ct):
    res = await ct.url_timeline("https://news.example.org/2026/08/flooded-airport")
    d = res["data"]
    types = [e["type"] for e in d["events"]]
    assert "first_archive_capture" in types and "declared_published_date_in_archived_copy" in types
    assert d["earliest_known"]["at"] == "2026-08-02T00:00:00Z"  # <time datetime="2026-08-02"> is earliest
    assert d["live"]["canonical"].endswith("flooded-airport")
    assert [e["at"] for e in d["events"]] == sorted(e["at"] for e in d["events"])


async def test_claim_timeline_and_fact_checks(ct):
    res = await ct.claim_timeline(claim="flooded airport photo", urls=["https://news.example.org/2026/08/flooded-airport"])
    d = res["data"]
    assert d["earliest_known_appearance"]["type"] == "claim_date_per_fact_checker"
    assert d["fact_checks"][0]["reviews"][0]["rating"] == "False"
    assert any(e["type"] == "fact_check_published" for e in d["timeline"])
    cr = await ct.claimreview_from_url("https://factcheck.example.org/flooded-airport")
    assert "Jane Poster" not in dumps(cr)


async def test_image_provenance_real_c2pa_samples(ct):
    pytest.importorskip("c2pa", reason="install the 'c2pa' extra")
    signed = await ct.image_provenance(str(FIXTURES / "c2pa" / "C.jpg"))
    c = signed["data"]["c2pa"]
    assert c["status"] == "manifest_found"
    assert c["signer"] == "C2PA Test Signing Cert"
    assert c["actions"][0]["action"] == "c2pa.created"
    assert c["actions"][0]["digital_source_type"] == "algorithmicMedia"
    assert c["declares_ai_generation"] is False
    assert any(i["code"] == "signingCredential.untrusted" for i in c["validation_issues"])
    plain = await ct.image_provenance(str(FIXTURES / "c2pa" / "A.jpg"))
    assert plain["data"]["c2pa"]["status"] == "no_manifest"
    remote = await ct.image_provenance(str(FIXTURES / "c2pa" / "cloud.jpg"))
    assert remote["data"]["c2pa"]["status"] in {"remote_manifest_unreachable", "manifest_found"}
    assert json.loads(json.dumps(remote))  # serialisable
