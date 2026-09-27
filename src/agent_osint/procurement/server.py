"""Procurement Red Flags MCP server."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..core.results import tool_errors
from ..core.runtime import configure_logging, get_runtime
from .service import Procurement

INSTRUCTIONS = """Procurement Red Flags loads public contracting data (OCDS: UK Find a Tender or any OCDS
publisher), computes integrity risk indicators (single bidder, non-open procedure, short tender, estimate
overruns, supplier concentration, shared addresses, split purchases, new or sanctioned suppliers) and profiles
buyers/suppliers. Flags are indicators for review — never state or imply corruption."""

mcp = FastMCP("procurement", instructions=INSTRUCTIONS)
_service: Procurement | None = None


def service() -> Procurement:
    global _service
    if _service is None:
        _service = Procurement(get_runtime("procurement"))
    return _service


@mcp.tool()
@tool_errors
async def load_ocds(path_or_url: Annotated[str, Field(description="Local path or URL of an OCDS release/record package (JSON or JSON Lines)")]) -> dict[str, Any]:
    """Load OCDS contracting data from any publisher."""
    return await service().load_ocds(path_or_url)


@mcp.tool()
@tool_errors
async def fetch_uk_awards(updated_from: Annotated[str, Field(description="YYYY-MM-DDTHH:MM:SS")],
                          updated_to: str | None = None,
                          max_pages: Annotated[int, Field(ge=1, le=50)] = 5) -> dict[str, Any]:
    """Pull award-stage notices from UK Find a Tender's OCDS API (100 per page)."""
    return await service().fetch_uk_awards(updated_from, updated_to, max_pages=max_pages)


@mcp.tool()
@tool_errors
def red_flags(buyer: str | None = None, supplier: str | None = None,
              rule: Annotated[str | None, Field(description="Filter to one rule id (see ruleset)")] = None,
              min_severity: Literal["low", "medium", "high", "critical"] = "low",
              limit: Annotated[int, Field(ge=1, le=500)] = 50) -> dict[str, Any]:
    """Awards with red flags, most severe first."""
    return service().red_flags(buyer=buyer, supplier=supplier, rule=rule, min_severity=min_severity, limit=limit)


@mcp.tool()
@tool_errors
def buyer_profile(buyer: Annotated[str, Field(description="Buyer id or name fragment")]) -> dict[str, Any]:
    """A buyer's awards, procedures, single-bid rate, supplier concentration (HHI) and flags."""
    return service().buyer_profile(buyer)


@mcp.tool()
@tool_errors
def supplier_profile(supplier: Annotated[str, Field(description="Supplier id, identifier or name fragment")]) -> dict[str, Any]:
    """A supplier's awards, buyers, flagged awards and registry/sanctions check results."""
    return service().supplier_profile(supplier)


@mcp.tool()
@tool_errors
async def check_supplier(name: str, country: Annotated[str | None, Field(description="ISO2 country")] = None,
                         company_number: Annotated[str | None, Field(description="UK Companies House number")] = None) -> dict[str, Any]:
    """Registry (Companies House / GLEIF) and sanctions (OpenSanctions) check for one organisation."""
    return await service().check_supplier(name, country=country, company_number=company_number)


@mcp.tool()
@tool_errors
async def enrich_suppliers(max_suppliers: Annotated[int, Field(ge=1, le=200)] = 20) -> dict[str, Any]:
    """Run registry + sanctions checks for loaded suppliers so new_supplier / sanctions_match flags apply."""
    return await service().enrich_suppliers(max_suppliers=max_suppliers)


@mcp.tool()
@tool_errors
def load_sanctions_file(path: Annotated[str, Field(description="OpenSanctions targets.simple.csv path")]) -> dict[str, Any]:
    """Load an OpenSanctions bulk CSV for offline screening (entity schemas only)."""
    return service().load_sanctions_file(path)


@mcp.tool()
@tool_errors
def ruleset(short_tender_days: int | None = None, concentration_share: float | None = None,
            split_threshold: float | None = None, new_supplier_days: int | None = None) -> dict[str, Any]:
    """Show (and optionally adjust) the red-flag rules and thresholds."""
    return service().set_params(short_tender_days=short_tender_days, concentration_share=concentration_share,
                                split_threshold=split_threshold, new_supplier_days=new_supplier_days)


@mcp.resource("procurement://evidence/{evidence_id}")
def evidence_resource(evidence_id: str) -> str:
    """Evidence record for loaded data."""
    return json.dumps(service().rt.evidence.get(evidence_id) or {"error": "unknown evidence id"}, indent=1)


def main(argv: list[str] | None = None) -> None:
    configure_logging()
    parser = argparse.ArgumentParser(prog="agent-osint-procurement")
    sub = parser.add_subparsers(dest="cmd")
    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    p_load = sub.add_parser("load")
    p_load.add_argument("path_or_url")
    args = parser.parse_args(argv)
    if args.cmd == "load":
        print(json.dumps(asyncio.run(service().load_ocds(args.path_or_url)), indent=1))
        return
    mcp.run(transport=getattr(args, "transport", "stdio"))


if __name__ == "__main__":
    main()
