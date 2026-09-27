"""IntegrityLens MCP server: research-integrity signals beyond retractions."""

from __future__ import annotations

import argparse
import json
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..core.results import tool_errors
from ..core.runtime import configure_logging, get_runtime
from .service import IntegrityLens

INSTRUCTIONS = """IntegrityLens checks scholarly works and journals for integrity signals: retractions,
expressions of concern and corrections (Crossref incl. Retraction Watch data), OpenAlex retraction flags,
unregistered DOIs (possible fabricated citations), PubPeer discussion counts, Problematic Paper Screener flags
and hijacked-journal lists (user-loaded), and tortured phrases. Report signals with their sources; never call
a paper 'clean' — say 'no signals in the sources checked'. Never characterise individuals."""

mcp = FastMCP("integritylens", instructions=INSTRUCTIONS)
_service: IntegrityLens | None = None


def service() -> IntegrityLens:
    global _service
    if _service is None:
        _service = IntegrityLens(get_runtime("integritylens"))
    return _service


@mcp.tool()
@tool_errors
async def integrity_report(doi: Annotated[str, Field(description="DOI or doi.org URL")]) -> dict[str, Any]:
    """All integrity signals for one work, each with its source, date and evidence."""
    return await service().integrity_report(doi)


@mcp.tool()
@tool_errors
async def screen_references(
    text: Annotated[str | None, Field(description="Reference list / BibTeX / any text containing DOIs")] = None,
    dois: Annotated[list[str] | None, Field(description="Explicit DOIs")] = None,
    max_items: Annotated[int, Field(ge=1, le=300)] = 100,
) -> dict[str, Any]:
    """Screen every DOI in a reference list; flags retractions, concerns and DOIs that do not exist."""
    return await service().screen_references(text, dois, max_items=max_items)


@mcp.tool()
@tool_errors
async def journal_legitimacy(
    query: Annotated[str, Field(description="ISSN (e.g. 1234-5678), journal website URL, or journal title")],
) -> dict[str, Any]:
    """Hijacked-journal list matches, OpenAlex/DOAJ status, homepage mismatch, retraction counts."""
    return await service().journal_legitimacy(query)


@mcp.tool()
@tool_errors
def scan_tortured_phrases(text: Annotated[str, Field(description="Abstract or full text")]) -> dict[str, Any]:
    """Detect 'tortured phrases' (e.g. 'counterfeit consciousness' for 'artificial intelligence')."""
    return service().scan_tortured_phrases(text)


@mcp.tool()
@tool_errors
def load_dataset(
    kind: Literal["retraction_watch", "problematic_paper_screener", "hijacked_journals"],
    path: Annotated[str, Field(description="CSV path on the server machine")],
) -> dict[str, Any]:
    """Load a locally downloaded dataset (Retraction Watch CSV, PPS export, hijacked-journal list)."""
    return service().load_dataset(kind, path)


@mcp.tool()
@tool_errors
def sources_status() -> dict[str, Any]:
    """Which sources are enabled / loaded."""
    return service().sources_status()


@mcp.resource("integritylens://evidence/{evidence_id}")
def evidence_resource(evidence_id: str) -> str:
    """Evidence record behind a signal."""
    return json.dumps(service().rt.evidence.get(evidence_id) or {"error": "unknown evidence id"}, indent=1)


def main(argv: list[str] | None = None) -> None:
    configure_logging()
    parser = argparse.ArgumentParser(prog="agent-osint-integritylens")
    sub = parser.add_subparsers(dest="cmd")
    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    p_load = sub.add_parser("load", help="load a local dataset")
    p_load.add_argument("kind", choices=["retraction_watch", "problematic_paper_screener", "hijacked_journals"])
    p_load.add_argument("path")
    args = parser.parse_args(argv)
    if args.cmd == "load":
        print(json.dumps(service().load_dataset(args.kind, args.path), indent=1))
        return
    mcp.run(transport=getattr(args, "transport", "stdio"))


if __name__ == "__main__":
    main()
