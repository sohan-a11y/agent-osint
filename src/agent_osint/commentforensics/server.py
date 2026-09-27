"""CommentForensics MCP server: form-letter and astroturf campaign detection in US rulemaking dockets."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..core.results import tool_errors
from ..core.runtime import configure_logging, get_runtime
from .service import CommentForensics

INSTRUCTIONS = """CommentForensics analyses public comments on Regulations.gov dockets. Workflow: docket_overview ->
ingest_docket (call repeatedly until complete; progress is saved) -> docket_summary / campaign_clusters ->
cluster_detail / campaign_sponsor_hints. Clusters are groups of near-identical text (form letters). Coordination
is lawful and common: describe clusters as coordinated campaigns, not fraud. Commenter names are never returned."""

mcp = FastMCP("commentforensics", instructions=INSTRUCTIONS)
_service: CommentForensics | None = None
DocketId = Annotated[str, Field(description="Regulations.gov docket ID, e.g. 'EPA-HQ-OAR-2021-0317'")]
Threshold = Annotated[float, Field(ge=0.5, le=1.0, description="Jaccard similarity threshold for grouping")]


def service() -> CommentForensics:
    global _service
    if _service is None:
        _service = CommentForensics(get_runtime("commentforensics"))
    return _service


@mcp.tool()
@tool_errors
async def docket_overview(docket_id: DocketId) -> dict[str, Any]:
    """Docket title/agency, its documents and reported comment totals, plus local ingestion status."""
    return await service().docket_overview(docket_id)


@mcp.tool()
@tool_errors
async def ingest_docket(
    docket_id: DocketId,
    source: Annotated[Literal["auto", "api", "mirrulations"], Field(
        description="'api' needs REGULATIONS_GOV_API_KEY; 'mirrulations' uses the public S3 mirror")] = "auto",
    max_requests: Annotated[int, Field(ge=1, le=2000, description="HTTP request budget for this call")] = 80,
) -> dict[str, Any]:
    """Download comments for a docket incrementally (resumable). Call again until 'complete' is true."""
    return await service().ingest(docket_id, source=source, max_requests=max_requests)


@mcp.tool()
@tool_errors
def ingest_bulk_csv(
    path: Annotated[str, Field(description="Path (on the server machine) to a Regulations.gov bulk-download CSV")],
    docket_id: Annotated[str | None, Field(description="Override docket ID if the CSV lacks one")] = None,
) -> dict[str, Any]:
    """Load comments from a Regulations.gov bulk-export CSV (fastest route for very large dockets)."""
    return service().ingest_csv(path, docket_id)


@mcp.tool()
@tool_errors
def docket_summary(docket_id: DocketId, threshold: Threshold = 0.8,
                   min_size: Annotated[int, Field(ge=2)] = 5) -> dict[str, Any]:
    """Totals, unique texts, share of comments in campaigns, largest campaigns, top organisations, daily volume."""
    return service().docket_summary(docket_id, threshold=threshold, min_size=min_size)


@mcp.tool()
@tool_errors
def campaign_clusters(docket_id: DocketId, threshold: Threshold = 0.8,
                      min_size: Annotated[int, Field(ge=2)] = 5,
                      limit: Annotated[int, Field(ge=1, le=200)] = 20) -> dict[str, Any]:
    """Near-duplicate comment clusters (form-letter campaigns) with template text and timing."""
    return service().campaign_clusters(docket_id, threshold=threshold, min_size=min_size, limit=limit)


@mcp.tool()
@tool_errors
def cluster_detail(docket_id: DocketId, cluster_id: str, threshold: Threshold = 0.8,
                   sample: Annotated[int, Field(ge=1, le=50)] = 10) -> dict[str, Any]:
    """One campaign: template text, sample comment links, organisations, posting timeline."""
    return service().cluster_detail(docket_id, cluster_id, threshold=threshold, sample=sample)


@mcp.tool()
@tool_errors
def campaign_sponsor_hints(docket_id: DocketId, cluster_id: str, threshold: Threshold = 0.8) -> dict[str, Any]:
    """Leads on who organised a campaign: organisation fields, domains, platform footers, affiliation phrases."""
    return service().sponsor_hints(docket_id, cluster_id, threshold=threshold)


@mcp.tool()
@tool_errors
def search_comments(docket_id: DocketId, phrase: str,
                    limit: Annotated[int, Field(ge=1, le=200)] = 20) -> dict[str, Any]:
    """Find stored comments containing a phrase (e.g. to trace a template sentence)."""
    return service().search_comments(docket_id, phrase, limit=limit)


@mcp.resource("commentforensics://evidence/{evidence_id}")
def evidence_resource(evidence_id: str) -> str:
    """Evidence record (URL, fetch time, SHA-256) behind stored comments."""
    return json.dumps(service().rt.evidence.get(evidence_id) or {"error": "unknown evidence id"}, indent=1)


def main(argv: list[str] | None = None) -> None:
    configure_logging()
    parser = argparse.ArgumentParser(prog="agent-osint-commentforensics")
    sub = parser.add_subparsers(dest="cmd")
    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    p_ing = sub.add_parser("ingest", help="ingest a docket to completion from the command line")
    p_ing.add_argument("docket_id")
    p_ing.add_argument("--source", default="auto", choices=["auto", "api", "mirrulations"])
    p_csv = sub.add_parser("ingest-csv")
    p_csv.add_argument("path")
    p_csv.add_argument("--docket-id")
    args = parser.parse_args(argv)
    if args.cmd == "ingest":
        async def run() -> None:
            svc = service()
            while True:
                res = await svc.ingest(args.docket_id, source=args.source, max_requests=500)
                print(json.dumps(res), flush=True)
                stopped = any(w.startswith("stopped early") for w in res.get("warnings", []))
                if not res.get("ok") or res["data"]["complete"] or stopped:
                    break
            await svc.rt.fetcher.aclose()
        asyncio.run(run())
        return
    if args.cmd == "ingest-csv":
        print(json.dumps(service().ingest_csv(args.path, args.docket_id), indent=1))
        return
    mcp.run(transport=getattr(args, "transport", "stdio"))


if __name__ == "__main__":
    main()
