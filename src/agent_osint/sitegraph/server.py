"""SiteGraph MCP server: who operates a website, and which sites share its operator."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..core.results import ok, tool_errors
from ..core.runtime import configure_logging, get_runtime
from .service import SiteGraph

INSTRUCTIONS = """SiteGraph attributes websites to the organisations that operate or monetise them and finds
sibling sites sharing operational fingerprints (ads.txt/sellers.json accounts, analytics/tag IDs, legal-notice
registry numbers, security.txt, certificates). Every link carries evidence IDs; read them via the
sitegraph://evidence/{id} resource. Scores are tiers (strong/likely/possible), never proof. Names of natural
persons are always withheld. Start with attribute_operator or fingerprint_site; use find_siblings to pivot."""

mcp = FastMCP("sitegraph", instructions=INSTRUCTIONS)
_service: SiteGraph | None = None


def service() -> SiteGraph:
    global _service
    if _service is None:
        _service = SiteGraph(get_runtime("sitegraph"))
    return _service


@mcp.tool()
@tool_errors
async def fingerprint_site(
    domain: Annotated[str, Field(description="Domain or URL, e.g. 'example.com'")],
    include_certificates: Annotated[bool, Field(description="Query Certificate Transparency for co-certified domains")] = True,
    refresh: Annotated[bool, Field(description="Ignore the 7-day fingerprint cache")] = False,
) -> dict[str, Any]:
    """Collect every operator fingerprint for a site: tracker/ad IDs, ads.txt + sellers.json records,
    security.txt, legal-notice entities and registry numbers, schema.org organisation, certificates."""
    fp = await service().fingerprint(domain, include_ct=include_certificates, refresh=refresh)
    evidence = fp.pop("evidence", [])
    return ok(fp, evidence=evidence)


@mcp.tool()
@tool_errors
async def attribute_operator(
    domain: Annotated[str, Field(description="Domain or URL")],
    refresh: Annotated[bool, Field(description="Ignore cached fingerprint")] = False,
) -> dict[str, Any]:
    """Rank candidate organisations that operate/own/monetise a site, with confidence and per-source evidence."""
    return await service().attribute_operator(domain, refresh=refresh)


@mcp.tool()
@tool_errors
async def find_siblings(
    query: Annotated[str, Field(description="A domain, or an identifier such as 'UA-12345', 'G-ABC123XYZ9', "
                                            "'GTM-ABC123', 'pub-1234567890123456', or 'adsystem.com, ACCOUNTID'")],
    limit: Annotated[int, Field(ge=1, le=200)] = 25,
    min_score: Annotated[float, Field(ge=0, le=1)] = 0.0,
) -> dict[str, Any]:
    """Find other domains in the index that share identifiers with a domain (or carry a given identifier)."""
    return await service().find_siblings(query, limit=limit, min_score=min_score)


@mcp.tool()
@tool_errors
async def compare_sites(
    domains: Annotated[list[str], Field(description="2-10 domains", min_length=2, max_length=10)],
    refresh: bool = False,
) -> dict[str, Any]:
    """Pairwise shared-fingerprint matrix with a scored verdict for each pair."""
    return await service().compare_sites(domains, refresh=refresh)


@mcp.tool()
@tool_errors
async def explain_link(a: str, b: str, refresh: bool = False) -> dict[str, Any]:
    """Human-readable evidence chain for why two sites are (or are not) linked."""
    return await service().explain_link(a, b, refresh=refresh)


@mcp.tool()
@tool_errors
async def index_domains(
    domains: Annotated[list[str], Field(description="Up to 25 domains to fingerprint into the index", max_length=25)],
    include_certificates: bool = False,
) -> dict[str, Any]:
    """Fingerprint a batch of domains so later sibling searches can find them."""
    return await service().index_domains(domains, include_ct=include_certificates)


@mcp.tool()
@tool_errors
async def site_history(domain: str) -> dict[str, Any]:
    """First/latest Wayback Machine capture and years with captures (a proxy for site age)."""
    return await service().site_history(domain)


@mcp.tool()
@tool_errors
async def index_stats() -> dict[str, Any]:
    """Size and composition of the local identifier index."""
    return ok(service().store.stats())


@mcp.resource("sitegraph://evidence/{evidence_id}")
def evidence_resource(evidence_id: str) -> str:
    """Raw evidence record (URL, fetch time, SHA-256, stored bytes) behind a claim."""
    rec = service().rt.evidence.get(evidence_id)
    return json.dumps(rec or {"error": "unknown evidence id"}, indent=1)


def main(argv: list[str] | None = None) -> None:
    configure_logging()
    parser = argparse.ArgumentParser(prog="agent-osint-sitegraph")
    sub = parser.add_subparsers(dest="cmd")
    p_serve = sub.add_parser("serve", help="run the MCP server (default)")
    p_serve.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    p_idx = sub.add_parser("index", help="fingerprint domains listed in a file (one per line)")
    p_idx.add_argument("file", type=Path)
    p_idx.add_argument("--ct", action="store_true")
    p_warc = sub.add_parser("ingest-warc", help="seed index from Common Crawl WARC files (local paths)")
    p_warc.add_argument("files", nargs="+", type=Path)
    p_warc.add_argument("--max-pages-per-host", type=int, default=3)
    args = parser.parse_args(argv)

    if args.cmd == "index":
        domains = [line.strip() for line in args.file.read_text().splitlines() if line.strip() and not line.startswith("#")]

        async def run() -> None:
            svc = service()
            for i in range(0, len(domains), 25):
                res = await svc.index_domains(domains[i:i + 25], include_ct=args.ct)
                for item in res["data"]["indexed"]:
                    print(json.dumps(item), flush=True)
            await svc.rt.fetcher.aclose()

        asyncio.run(run())
        return
    if args.cmd == "ingest-warc":
        from .cc_ingest import ingest_warc

        svc = service()
        for f in args.files:
            stats = ingest_warc(f, svc.store, svc.rt.evidence, max_pages_per_host=args.max_pages_per_host)
            print(json.dumps({"file": str(f), **stats}), file=sys.stdout)
        return
    transport = getattr(args, "transport", "stdio")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
