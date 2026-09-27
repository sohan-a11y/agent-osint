"""Live smoke test: exercise every server against the real upstream services.

Run on a machine with normal internet access:

    python scripts/live_smoke.py                      # all tools
    python scripts/live_smoke.py --only depcheck sitegraph
    AGENT_OSINT_RECORD_DIR=./recorded python scripts/live_smoke.py   # also capture fixtures

Checks that need credentials are skipped (and reported) when the environment variable is missing.
Exit code is 0 only if every non-skipped check passed. Each check asserts on real-world facts that are
stable (e.g. the Wakefield 1998 paper is retracted), so a failure means an integration broke.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
import traceback
from collections.abc import Awaitable, Callable
from typing import Any

from agent_osint.core.runtime import get_runtime, reset_runtimes

RESULTS: list[tuple[str, str, str, float]] = []


def need(var: str) -> str | None:
    return None if os.environ.get(var) else f"skipped: {var} not set"


async def check(tool: str, name: str, fn: Callable[[], Awaitable[Any]], skip: str | None = None) -> None:
    if skip:
        RESULTS.append((tool, name, skip, 0.0))
        return
    t0 = time.time()
    try:
        await fn()
        RESULTS.append((tool, name, "PASS", time.time() - t0))
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((tool, name, f"FAIL: {type(exc).__name__}: {exc}", time.time() - t0))
        if os.environ.get("SMOKE_DEBUG"):
            traceback.print_exc()


def expect(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


async def depcheck() -> None:
    from agent_osint.depcheck.service import DepCheck

    dc = DepCheck(get_runtime("depcheck"))

    async def pypi() -> None:
        r = await dc.check_packages("pypi", ["requests", "reqeusts"])
        by = {x["name"]: x for x in r["data"]["results"]}
        expect(by["requests"]["verdict"] == "ok", "requests should be ok")
        expect(by["reqeusts"]["verdict"] == "block", "reqeusts should be blocked")

    async def npm() -> None:
        r = await dc.check_packages("npm", ["react", "crossenv"])
        by = {x["name"]: x for x in r["data"]["results"]}
        expect(by["react"]["verdict"] == "ok", "react ok")
        expect(by["crossenv"]["verdict"] == "block", "crossenv (npm security placeholder) blocked")

    await check("depcheck", "PyPI lookups", pypi)
    await check("depcheck", "npm lookups", npm)


async def integritylens() -> None:
    from agent_osint.integritylens.service import IntegrityLens

    il = IntegrityLens(get_runtime("integritylens"))

    async def retracted() -> None:
        r = await il.integrity_report("10.1016/S0140-6736(97)11096-0")
        expect(r["ok"] and r["data"]["status"] == "retracted_or_removed", f"Wakefield not flagged: {r}")

    async def clean() -> None:
        r = await il.integrity_report("10.1038/171737a0")
        expect(r["ok"] and r["data"]["status"] == "no_signals_in_checked_sources", f"Watson-Crick flagged: {r}")

    async def fake() -> None:
        r = await il.integrity_report("10.9999/this.doi.does.not.exist.2026")
        expect(any(s["kind"] == "doi_not_registered" for s in r["data"]["signals"]), "fake DOI not detected")

    async def journal() -> None:
        r = await il.journal_legitimacy("0028-0836")
        expect(r["ok"] and r["data"]["venue"] and "Nature" in r["data"]["venue"]["name"], f"Nature not found: {r}")

    async def pubpeer() -> None:
        r = await il.integrity_report("10.1016/S0140-6736(20)31180-6")
        expect(any(s["kind"] == "pubpeer_discussion" for s in r["data"]["signals"]), "no PubPeer signal")

    await check("integritylens", "Crossref retraction (Wakefield 1998)", retracted)
    await check("integritylens", "Crossref clean control (Watson & Crick 1953)", clean)
    await check("integritylens", "doi.org unregistered DOI", fake)
    await check("integritylens", "OpenAlex journal lookup", journal)
    await check("integritylens", "PubPeer counts", pubpeer, need("PUBPEER_DEVKEY"))


async def sitegraph(domain: str) -> None:
    from agent_osint.sitegraph.service import SiteGraph

    sg = SiteGraph(get_runtime("sitegraph"))

    async def fp() -> None:
        r = await sg.fingerprint(domain, refresh=True)
        expect(r.get("ads_txt") or r.get("homepage"), f"no homepage/ads.txt for {domain}: {r.get('errors')}")
        print(f"    {domain}: identifiers={r['identifiers']} errors={list(r['errors'])}")

    async def attr() -> None:
        r = await sg.attribute_operator(domain)
        expect(r["ok"] and r["data"]["candidates"], f"no operator candidates: {str(r)[:300]}")
        print(f"    top candidates: {[(c['organization'], c['domains'], c['confidence']) for c in r['data']['candidates'][:3]]}")

    async def hist() -> None:
        r = await sg.site_history(domain)
        expect(r["data"]["first_capture"] is not None, "no Wayback captures")

    await check("sitegraph", f"fingerprint {domain} (incl. sellers.json, CT)", fp)
    await check("sitegraph", f"attribute_operator {domain}", attr)
    await check("sitegraph", "Wayback site_history", hist)


async def commentforensics(docket: str) -> None:
    from agent_osint.commentforensics.service import CommentForensics

    cf = CommentForensics(get_runtime("commentforensics"))

    async def mirror() -> None:
        r = await cf.ingest(docket, source="mirrulations", max_requests=60)
        expect(r["ok"] and r["data"]["known_comments"] > 0, f"no comments from Mirrulations for {docket}: {r}")

    async def api() -> None:
        r = await cf.docket_overview(docket)
        expect(r["ok"] and r["data"].get("documents"), f"no documents: {r}")

    await check("commentforensics", f"Mirrulations ingest {docket}", mirror)
    await check("commentforensics", f"Regulations.gov docket_overview {docket}", api, need("REGULATIONS_GOV_API_KEY"))


async def dsalens() -> None:
    from agent_osint.dsalens.service import DSALens

    ds = DSALens(get_runtime("dsalens"))
    day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 7 * 86400))

    async def agg() -> None:
        r = await ds.moderation_stats(group_by="automated_decision", date_from=day, date_to=day, source="api")
        expect(r["ok"] and r["data"]["total_statements"] > 0, str(r))

    async def ads() -> None:
        r = await ds.ad_search(countries=["DE"], search_terms="energie", limit=5)
        expect(r["ok"] and r["data"]["returned"] > 0, str(r))

    await check("dsalens", "Research API aggregates", agg, need("DSA_TDB_TOKEN"))
    await check("dsalens", "Meta Ad Library search", ads, need("META_AD_LIBRARY_TOKEN"))


async def procurement() -> None:
    from agent_osint.procurement.service import Procurement

    pr = Procurement(get_runtime("procurement"))
    since = time.strftime("%Y-%m-%dT00:00:00", time.gmtime(time.time() - 3 * 86400))

    async def fts() -> None:
        r = await pr.fetch_uk_awards(since, max_pages=1)
        expect(r["ok"] and r["data"]["releases"] > 0, str(r))
        flags = pr.red_flags(limit=3)
        expect(flags["ok"], str(flags))

    async def gleif() -> None:
        r = await pr.check_supplier("Balfour Beatty plc", country="GB")
        expect(r["ok"] and r["data"]["registry_matches"], str(r))

    await check("procurement", "UK Find a Tender OCDS fetch + flags", fts)
    await check("procurement", "GLEIF / Companies House supplier check", gleif)


async def claimtrail() -> None:
    from agent_osint.claimtrail.service import ClaimTrail

    ct = ClaimTrail(get_runtime("claimtrail"))

    async def timeline() -> None:
        r = await ct.url_timeline("https://www.example.com/")
        expect(r["ok"] and any(e["type"] == "first_archive_capture" for e in r["data"]["events"]), str(r)[:300])

    async def fc() -> None:
        r = await ct.fact_checks("5G coronavirus")
        expect(r["ok"] and r["data"]["claims"], str(r)[:300])

    await check("claimtrail", "Wayback + live page timeline", timeline)
    await check("claimtrail", "Google Fact Check search", fc, need("GOOGLE_FACTCHECK_API_KEY"))


async def main(only: list[str], domain: str, docket: str) -> int:
    os.environ.setdefault("AGENT_OSINT_HOME", tempfile.mkdtemp(prefix="agent-osint-smoke-"))
    os.environ.pop("AGENT_OSINT_REPLAY_DIR", None)
    reset_runtimes()
    suites = {"depcheck": depcheck, "integritylens": integritylens, "sitegraph": lambda: sitegraph(domain),
              "commentforensics": lambda: commentforensics(docket), "dsalens": dsalens,
              "procurement": procurement, "claimtrail": claimtrail}
    for name, fn in suites.items():
        if not only or name in only:
            print(f"== {name}")
            await fn()
    print("\nRESULTS")
    failed = 0
    for tool, name, status, secs in RESULTS:
        print(f"  {tool:17} {name:52} {status}  ({secs:.1f}s)")
        failed += status.startswith("FAIL")
    print(f"\n{sum(r[2] == 'PASS' for r in RESULTS)} passed, {failed} failed, "
          f"{sum(r[2].startswith('skipped') for r in RESULTS)} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=[])
    ap.add_argument("--domain", default="theguardian.com", help="site for SiteGraph checks")
    ap.add_argument("--docket", default="EPA-HQ-OAR-2021-0317", help="docket for CommentForensics checks")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.only, a.domain, a.docket)))
