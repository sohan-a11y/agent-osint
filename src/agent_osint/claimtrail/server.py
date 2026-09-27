"""ClaimTrail MCP server: provenance timelines for claims, pages and images."""

from __future__ import annotations

import argparse
import json
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..core.results import tool_errors
from ..core.runtime import configure_logging, get_runtime
from .service import ClaimTrail

INSTRUCTIONS = """ClaimTrail builds provenance timelines: earliest Internet Archive captures, page-declared publish
dates (live and in the earliest archived copy), fact-checks (Google Fact Check Tools / ClaimReview markup) and C2PA
Content Credentials for images. 'Earliest known' is bounded by coverage; say so."""

mcp = FastMCP("claimtrail", instructions=INSTRUCTIONS)
_service: ClaimTrail | None = None


def service() -> ClaimTrail:
    global _service
    if _service is None:
        _service = ClaimTrail(get_runtime("claimtrail"))
    return _service


@mcp.tool()
@tool_errors
async def claim_timeline(claim: Annotated[str | None, Field(description="Claim text to look up in fact-checks")] = None,
                         urls: Annotated[list[str] | None, Field(description="Up to 10 URLs where it appeared", max_length=10)] = None,
                         language: str | None = None) -> dict[str, Any]:
    """Merged, dated timeline for a claim across URLs, archives and fact-checks."""
    return await service().claim_timeline(claim=claim, urls=urls, language=language)


@mcp.tool()
@tool_errors
async def url_timeline(url: str) -> dict[str, Any]:
    """First/latest archive capture, declared publish dates (live + earliest archived copy), removal status."""
    return await service().url_timeline(url)


@mcp.tool()
@tool_errors
async def fact_checks(query: str, language: str | None = None,
                      max_age_days: Annotated[int | None, Field(ge=1)] = None) -> dict[str, Any]:
    """Search published fact-checks (Google Fact Check Tools API; needs GOOGLE_FACTCHECK_API_KEY)."""
    return await service().fact_checks(query, language=language, max_age_days=max_age_days)


@mcp.tool()
@tool_errors
async def claimreview_from_url(url: str) -> dict[str, Any]:
    """Extract schema.org ClaimReview data (claim, rating, dates, appearances) from a fact-check article."""
    return await service().claimreview_from_url(url)


@mcp.tool()
@tool_errors
async def image_provenance(source: Annotated[str, Field(description="Image/video URL or local path")]) -> dict[str, Any]:
    """Read C2PA Content Credentials: signer, time, actions, AI-generation declarations, validation state."""
    return await service().image_provenance(source)


@mcp.resource("claimtrail://evidence/{evidence_id}")
def evidence_resource(evidence_id: str) -> str:
    """Evidence record behind a timeline event."""
    return json.dumps(service().rt.evidence.get(evidence_id) or {"error": "unknown evidence id"}, indent=1)


def main(argv: list[str] | None = None) -> None:
    configure_logging()
    parser = argparse.ArgumentParser(prog="agent-osint-claimtrail")
    sub = parser.add_subparsers(dest="cmd")
    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    args = parser.parse_args(argv)
    mcp.run(transport=getattr(args, "transport", "stdio"))


if __name__ == "__main__":
    main()
