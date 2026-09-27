"""SiteGraph end-to-end tests against a synthetic three-site network served from replay fixtures.

The fixture formats follow the IAB ads.txt 1.1 / sellers.json 1.0 specs, RFC 9116 and the
Cert Spotter / Wayback CDX JSON shapes. The sites and identifiers are invented.
"""

from __future__ import annotations

import io
import json

import pytest

from agent_osint.sitegraph.adstxt import parse_ads_txt
from agent_osint.sitegraph.extract import extract_page
from agent_osint.sitegraph.imprint import parse_imprint
from agent_osint.sitegraph.securitytxt import parse_security_txt
from agent_osint.sitegraph.service import SiteGraph
from tests.conftest import dumps

ALPHA = "alpha-daily-news.com"
BETA = "beta-daily-news.com"
UNREL = "unrelated-garden-blog.com"

ALPHA_HTML = """<!doctype html><html><head><title>Alpha Daily News</title>
<meta property="og:site_name" content="Alpha Daily">
<meta name="facebook-domain-verification" content="abcdefghij0123456789klmnop">
<script async src="https://www.googletagmanager.com/gtag/js?id=G-ABCDEF1234"></script>
<script>window.dataLayer=[];function gtag(){dataLayer.push(arguments)};gtag('config','G-ABCDEF1234');</script>
<script>(function(w,d,s,l,i){})(window,document,'script','dataLayer','GTM-ABC1234');</script>
<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-1111222233334444"></script>
<script>!function(f,b,e,v,n,t,s){}(window);fbq('init', '314159265358979');</script>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"NewsArticle",
 "publisher":{"@type":"NewsMediaOrganization","name":"Example Media Group Ltd","url":"https://example-media.com"}}</script>
</head><body><p>We track UA-99999999-1 in this article text, which must not be extracted.</p>
<footer><a href="/impressum">Impressum</a> <a href="/about-us">About us</a>
© 2026 Example Media Group Ltd. All rights reserved.</footer></body></html>"""

ALPHA_IMPRINT = """<html><body><main><h1>Impressum</h1>
<p>Example Media Group GmbH<br>Musterstraße 1, 80331 München</p>
<p>Geschäftsführer: Max Mustermann</p>
<p>Registergericht: Amtsgericht München, HRB 123456</p>
<p>USt-IdNr.: DE123456789</p>
<p>E-Mail: info@example-media.com, max.mustermann@example-media.com, Tel. +49 89 7654321</p>
</main></body></html>"""

ALPHA_ADS = """# ads.txt for alpha
OWNERDOMAIN=example-media.com
MANAGERDOMAIN=bigmanager.com
CONTACT=adops@example-media.com
google.com, pub-1111222233334444, DIRECT, f08c47fec0942fa0
appnexus.com, 9999, DIRECT
rubiconproject.com, 11111, RESELLER, 0bfd66d529a55807 # reseller
this line is invalid
"""

BETA_HTML = """<html><head><title>Beta Daily</title>
<script>(function(w,d,s,l,i){})(window,document,'script','dataLayer','GTM-ABC1234');</script>
<script type="application/ld+json">[{"@type":"Organization","name":"Example Media Group Ltd"}]</script>
</head><body><footer>Copyright 2025 Example Media Group Ltd</footer></body></html>"""

BETA_ADS = """OWNERDOMAIN=example-media.com
google.com, pub-1111222233334444, DIRECT, f08c47fec0942fa0
"""

UNREL_HTML = """<html><head><title>Garden</title>
<script>gtag('config','G-ZZZZZZ9999');</script></head><body>© Jane Gardener</body></html>"""

GOOGLE_SELLERS = {
    "contact_email": "sellers@google.example", "version": "1.0",
    "sellers": [
        {"seller_id": "pub-1111222233334444", "seller_type": "PUBLISHER", "name": "Example Media Group GmbH",
         "domain": "example-media.com"},
        {"seller_id": "pub-5555666677778888", "seller_type": "PUBLISHER", "name": "John Smith",
         "domain": "johnsmith-blog.com"},
        {"seller_id": "pub-0000000000000001", "seller_type": "PUBLISHER", "is_confidential": 1},
    ],
}
APPNEXUS_SELLERS = {"version": "1.0", "sellers": [
    {"seller_id": "9999", "seller_type": "INTERMEDIARY", "name": "Big Manager Inc", "domain": "bigmanager.com"}]}

SECURITY_TXT = """-----BEGIN PGP SIGNED MESSAGE-----
Hash: SHA256

Contact: mailto:security@example-media.com
Contact: https://hackerone.com/examplemedia
Contact: tel:+49-89-7654321
Expires: 2027-01-01T00:00:00.000Z
Canonical: https://alpha-daily-news.com/.well-known/security.txt
Policy: https://example-media.com/security-policy
-----BEGIN PGP SIGNATURE-----
abc
-----END PGP SIGNATURE-----
"""

CERTSPOTTER = [
    {"id": "1001", "cert_sha256": "aa" * 32, "dns_names": [ALPHA, f"www.{ALPHA}", BETA, f"www.{BETA}"],
     "not_before": "2026-01-01T00:00:00Z", "not_after": "2026-12-31T00:00:00Z",
     "issuer": {"friendly_name": "Let's Encrypt"}},
    {"id": "1002", "cert_sha256": "bb" * 32,
     "dns_names": [ALPHA] + [f"cdn-customer-{i}.com" for i in range(20)],
     "not_before": "2026-01-01T00:00:00Z", "not_after": "2026-12-31T00:00:00Z"},
]

CDX_FIRST = [["timestamp", "original", "statuscode", "digest", "mimetype"],
             ["20190312101010", f"http://{ALPHA}/", "200", "ABC", "text/html"]]
CDX_LAST = [["timestamp", "original", "statuscode", "digest", "mimetype"],
            ["20260901000000", f"https://{ALPHA}/", "200", "DEF", "text/html"]]
CDX_YEARLY = [["timestamp", "original", "statuscode", "digest", "mimetype"]] + [
    [f"{y}0101000000", f"http://{ALPHA}/", "200", "X", "text/html"] for y in range(2019, 2027)]


def _serve_network(replay) -> None:
    r = replay
    html = {"content-type": "text/html; charset=utf-8"}
    text = {"content-type": "text/plain"}
    # alpha
    r.add(f"https://{ALPHA}/robots.txt", status=404)
    r.add(f"https://{ALPHA}/", ALPHA_HTML, headers=html)
    r.add(f"https://{ALPHA}/impressum", ALPHA_IMPRINT, headers=html)
    r.add(f"https://{ALPHA}/ads.txt", ALPHA_ADS, headers=text)
    r.add(f"https://{ALPHA}/app-ads.txt", status=404)
    r.add(f"http://{ALPHA}/app-ads.txt", status=404)
    r.add(f"https://{ALPHA}/.well-known/security.txt", SECURITY_TXT, headers=text)
    r.add("https://api.certspotter.com/v1/issuances?domain=alpha-daily-news.com&include_subdomains=true"
          "&expand=dns_names&expand=issuer", CERTSPOTTER)
    # beta: ads.txt reached via an in-domain redirect
    r.add(f"https://{BETA}/robots.txt", "User-agent: *\nAllow: /\n", headers=text)
    r.add(f"https://{BETA}/", BETA_HTML, headers=html)
    r.add(f"https://{BETA}/ads.txt", status=301, headers={"location": f"https://www.{BETA}/ads.txt"})
    r.add(f"https://www.{BETA}/ads.txt", BETA_ADS, headers=text)
    r.add(f"https://{BETA}/app-ads.txt", status=404)
    r.add(f"http://{BETA}/app-ads.txt", status=404)
    r.add(f"https://{BETA}/.well-known/security.txt", status=404)
    r.add(f"https://{BETA}/security.txt", "<!doctype html><html>not found</html>", headers=html)
    r.add("https://api.certspotter.com/v1/issuances?domain=beta-daily-news.com&include_subdomains=true"
          "&expand=dns_names&expand=issuer", [CERTSPOTTER[0]])
    # unrelated: robots disallows everything
    r.add(f"https://{UNREL}/robots.txt", "User-agent: *\nDisallow: /\n", headers=text)
    r.add(f"https://{UNREL}/", UNREL_HTML, headers=html)
    r.add(f"https://{UNREL}/ads.txt", status=404)
    r.add(f"http://{UNREL}/ads.txt", "<html>soft 404</html>", headers=html)
    # sellers.json
    r.add("https://realtimebidding.google.com/sellers.json", GOOGLE_SELLERS)
    r.add("https://appnexus.com/sellers.json", APPNEXUS_SELLERS)
    # wayback
    base = "https://web.archive.org/cdx/search/cdx?url=alpha-daily-news.com&output=json&fl=timestamp,original,statuscode,digest,mimetype&filter=statuscode:200"
    r.add(base + "&limit=1", CDX_FIRST)
    r.add(base + "&limit=-1", CDX_LAST)
    r.add(base + "&limit=100&collapse=timestamp:4", CDX_YEARLY)


# ---------------------------------------------------------------- unit tests

def test_parse_ads_txt_spec_cases():
    parsed = parse_ads_txt(ALPHA_ADS)
    assert [r.relationship for r in parsed.records] == ["DIRECT", "DIRECT", "RESELLER"]
    assert parsed.records[0].cert_id == "f08c47fec0942fa0"
    assert parsed.owner_domains() == ["example-media.com"]
    assert parsed.manager_domains() == [{"domain": "bigmanager.com"}]
    assert parsed.invalid_lines == 1
    assert parse_ads_txt("MANAGERDOMAIN=mgr.com, DE\ngoogle.com, pub-1, direct").manager_domains() == [
        {"domain": "mgr.com", "country": "DE"}]
    assert parse_ads_txt("google.com, pub-1, direct").records[0].relationship == "DIRECT"  # case-insensitive


def test_extract_page_only_reads_code_not_text():
    sig = extract_page(ALPHA_HTML, f"https://{ALPHA}/")
    assert sig.ids["gtm"] == {"GTM-ABC1234"}
    assert sig.ids["ga4"] == {"G-ABCDEF1234"}
    assert sig.ids["adsense"] == {"pub-1111222233334444"}
    assert sig.ids["fb_pixel"] == {"314159265358979"}
    assert "ga_ua" not in sig.ids  # UA id appears only in visible text
    assert sig.jsonld_orgs[0]["name"] == "Example Media Group Ltd"
    assert sig.copyright_holders[0].startswith("Example Media Group Ltd")
    assert sig.imprint_links[0] == f"https://{ALPHA}/impressum"


def test_imprint_parsing_withholds_people():
    facts = parse_imprint(ALPHA_IMPRINT)
    assert "Example Media Group GmbH" in facts.entities
    assert facts.registration_numbers[0]["value"] == "HRB 123456"
    assert facts.registration_numbers[0]["court"] == "München"
    assert facts.vat_ids == ["DE123456789"]
    assert facts.role_emails == ["info@example-media.com"]
    assert "Mustermann" not in dumps(facts.__dict__)


def test_security_txt_signed_and_platforms_excluded():
    st = parse_security_txt(SECURITY_TXT)
    assert st.signed
    assert "tel:" not in dumps(st.contacts())
    assert st.linking_domains() == ["example-media.com", "alpha-daily-news.com"]


# ---------------------------------------------------------------- service tests

@pytest.fixture
def graph(replay):
    _serve_network(replay)
    return SiteGraph(replay.runtime("sitegraph"))


async def test_fingerprint_and_attribution(graph):
    fp = await graph.fingerprint(ALPHA)
    assert fp["identifiers"]["gtm"] == ["GTM-ABC1234"]
    assert fp["ads_txt"]["owner_domains"] == ["example-media.com"]
    assert fp["imprint"]["registration_numbers"][0]["key"].startswith("DE-HR:")
    sellers = {s["ad_system"]: s for s in fp["sellers"]}
    assert sellers["google.com"]["name"] == "Example Media Group GmbH"
    assert sellers["appnexus.com"]["seller_type"] == "INTERMEDIARY"
    assert fp["certificates"]["co_certified_domains"] == {BETA: 1}
    assert fp["certificates"]["shared_or_cdn_certificates"] == 1

    res = await graph.attribute_operator(ALPHA)
    assert res["ok"]
    top = res["data"]["candidates"][0]
    assert "example-media.com" in top["domains"]
    assert top["confidence_tier"] == "high"
    assert top["independent_sources"] >= 3
    blob = dumps(res)
    for secret in ("Mustermann", "max.mustermann", "7654321", "John Smith"):
        assert secret not in blob
    roles = {c["organization"]: c["roles"] for c in res["data"]["candidates"]}
    assert roles.get("Big Manager Inc") == ["monetisation_manager"]
    assert all(ev.get("sha256") or ev.get("note") for ev in res["evidence"])


async def test_siblings_compare_explain(graph):
    await graph.fingerprint(ALPHA)
    await graph.fingerprint(BETA)
    unrel = await graph.fingerprint(UNREL)
    assert "robots" in unrel["errors"]["homepage"]
    assert "HTML page" in unrel["errors"]["ads_txt"] or "HTTP" in unrel["errors"]["ads_txt"]

    sib = await graph.find_siblings(ALPHA)
    sibs = {s["domain"]: s for s in sib["data"]["siblings"]}
    assert BETA in sibs and UNREL not in sibs
    assert sibs[BETA]["tier"] == "strong"
    kinds = {s["kind"] for s in sibs[BETA]["shared"]}
    assert {"adstxt_ownerdomain", "google_pub", "gtm", "ct_cert"} <= kinds
    # CT peers that were never fingerprinted are still discoverable (single family => possible)
    assert "cdn-customer-1.com" not in sibs  # shared CDN certificate ignored

    by_id = await graph.find_siblings("GTM-ABC1234")
    assert {s["domain"] for s in by_id["data"]["siblings"]} == {ALPHA, BETA}
    by_pub = await graph.find_siblings("ca-pub-1111222233334444")
    assert {s["domain"] for s in by_pub["data"]["siblings"]} == {ALPHA, BETA}

    cmp_ = await graph.compare_sites([ALPHA, BETA, UNREL])
    top = cmp_["data"]["pairs"][0]
    assert {top["a"], top["b"]} == {ALPHA, BETA} and top["tier"] == "strong"
    assert cmp_["data"]["pairs"][-1]["tier"] == "none"

    ex = await graph.explain_link(ALPHA, BETA)
    assert any("GTM-ABC1234" in s for s in ex["data"]["explanation"])
    assert ex["evidence"]


async def test_single_family_is_only_possible(graph, replay):
    await graph.fingerprint(ALPHA)
    replay.add("https://gamma-daily-news.com/robots.txt", status=404)
    replay.add("https://gamma-daily-news.com/", "<script>x('GTM-ABC1234')</script>",
               headers={"content-type": "text/html"})
    fp = await graph.fingerprint("gamma-daily-news.com", include_ct=False)
    assert fp["identifiers"] == {"gtm": ["GTM-ABC1234"]}
    sib = await graph.find_siblings("gamma-daily-news.com")
    alpha = next(s for s in sib["data"]["siblings"] if s["domain"] == ALPHA)
    assert alpha["tier"] == "possible"
    assert alpha["notes"]


async def test_site_history(graph):
    res = await graph.site_history(f"https://www.{ALPHA}/some/page")
    assert res["data"]["first_capture"]["at"] == "2019-03-12T10:10:10Z"
    assert res["data"]["years_with_captures"][0] == "2019"


async def test_common_crawl_ingest(graph, tmp_path):
    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    from agent_osint.sitegraph.cc_ingest import ingest_warc

    await graph.fingerprint(ALPHA)
    path = tmp_path / "sample.warc.gz"
    with path.open("wb") as fh:
        writer = WARCWriter(fh, gzip=True)
        for host, html in [("delta-daily-news.com", "<script>gtag('config','G-ABCDEF1234')</script>"),
                           ("noise.com", "<p>nothing</p>")]:
            http_headers = StatusAndHeaders("200 OK", [("Content-Type", "text/html")], protocol="HTTP/1.1")
            rec = writer.create_warc_record(f"https://{host}/", "response", payload=io.BytesIO(html.encode()),
                                            http_headers=http_headers)
            writer.write_record(rec)
    stats = ingest_warc(path, graph.store, graph.rt.evidence)
    assert stats["html_records"] == 2 and stats["signals"] >= 1
    sib = await graph.find_siblings(ALPHA)
    delta = next(s for s in sib["data"]["siblings"] if s["domain"] == "delta-daily-news.com")
    assert delta["shared"][0]["value"] == "G-ABCDEF1234"
    ev = graph.rt.evidence.get(delta["evidence_ids"][0])
    assert "Common Crawl WARC sample.warc.gz" in ev["note"]
    assert json.loads(json.dumps(graph.store.stats()))["domains_by_source"]["commoncrawl"] == 1
