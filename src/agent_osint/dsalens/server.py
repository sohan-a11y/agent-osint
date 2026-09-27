"""DSA-Lens MCP server: EU DSA Transparency Database statistics and platform ad repositories."""

from __future__ import annotations

import argparse
import json
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..core.results import tool_errors
from ..core.runtime import configure_logging, get_runtime
from .service import GROUPABLE, DSALens

INSTRUCTIONS = """DSA-Lens answers questions about platform content moderation in the EU using the DSA Transparency
Database (statements of reasons) via the Research API (last 6 months, needs DSA_TDB_TOKEN) or locally ingested
daily dumps (any period), plus Meta's Ad Library (needs META_AD_LIBRARY_TOKEN). Numbers are platform self-reports;
say so when comparing platforms."""

mcp = FastMCP("dsalens", instructions=INSTRUCTIONS)
_service: DSALens | None = None
Date = Annotated[str, Field(description="YYYY-MM-DD", pattern=r"^\d{4}-\d{2}-\d{2}$")]
Source = Annotated[Literal["auto", "api", "local"], Field(description="'api' = Research API, 'local' = ingested dumps")]


def service() -> DSALens:
    global _service
    if _service is None:
        _service = DSALens(get_runtime("dsalens"))
    return _service


@mcp.tool()
@tool_errors
async def moderation_stats(
    group_by: Annotated[str, Field(description=f"One of {GROUPABLE}")],
    date_from: Date, date_to: Date,
    platform: Annotated[str | None, Field(description="Platform name (e.g. 'TikTok') or numeric platform_id")] = None,
    country: Annotated[str | None, Field(description="ISO country in territorial_scope (local source only)")] = None,
    source: Source = "auto",
    limit: Annotated[int, Field(ge=1, le=200)] = 30,
) -> dict[str, Any]:
    """Counts of statements of reasons grouped by a field (category, decision ground, automation, ...)."""
    return await service().moderation_stats(group_by=group_by, date_from=date_from, date_to=date_to, platform=platform,
                                            country=country, source=source, limit=limit)


@mcp.tool()
@tool_errors
async def compare_platforms(date_from: Date, date_to: Date,
                            platforms: Annotated[list[str] | None, Field(description="Platform names to include")] = None,
                            source: Source = "auto", top: Annotated[int, Field(ge=1, le=100)] = 15) -> dict[str, Any]:
    """Per-platform volume, share of fully automated decisions, automated detection, illegal-content ground, top categories."""
    return await service().compare_platforms(date_from=date_from, date_to=date_to, platforms=platforms, source=source,
                                             top=top)


@mcp.tool()
@tool_errors
async def count_statements(
    date_from: Date | None = None, date_to: Date | None = None, platform_id: int | None = None,
    filters: Annotated[dict[str, Any] | None, Field(description="Exact-match terms, e.g. {'category': "
                                                                "'STATEMENT_CATEGORY_SCAMS_AND_FRAUD'}")] = None,
) -> dict[str, Any]:
    """Exact count via the Research API /count endpoint (requires DSA_TDB_TOKEN)."""
    return await service().count_statements(date_from=date_from, date_to=date_to, platform_id=platform_id,
                                            filters=filters)


@mcp.tool()
@tool_errors
async def sample_statements(n: Annotated[int, Field(ge=1, le=100)] = 10, date_from: Date | None = None,
                            date_to: Date | None = None, platform: str | None = None, platform_id: int | None = None,
                            country: str | None = None, filters: dict[str, str] | None = None,
                            source: Source = "auto") -> dict[str, Any]:
    """A sample of individual statements of reasons (UUIDs and key fields) for qualitative review."""
    return await service().sample_statements(n=n, date_from=date_from, date_to=date_to, platform=platform,
                                             platform_id=platform_id, country=country, filters=filters, source=source)


@mcp.tool()
@tool_errors
def ingest_dump(path: Annotated[str, Field(description="Local path to a daily dump .zip (or extracted .csv)")],
                platforms: Annotated[list[str] | None, Field(description="Only keep these platform names")] = None,
                max_rows: int | None = None) -> dict[str, Any]:
    """Load a DSA Transparency Database daily dump into the local analytical store."""
    return service().ingest_dump(path, platforms=platforms, max_rows=max_rows)


@mcp.tool()
@tool_errors
def dataset_status() -> dict[str, Any]:
    """Configured credentials and local data coverage."""
    return service().dataset_status()


@mcp.tool()
@tool_errors
async def ad_library_search(
    countries: Annotated[list[str], Field(description="ISO codes, e.g. ['DE','FR']", min_length=1)],
    search_terms: str | None = None, page_ids: list[str] | None = None,
    ad_type: Literal["ALL", "POLITICAL_AND_ISSUE_ADS", "EMPLOYMENT_ADS", "HOUSING_ADS",
                     "FINANCIAL_PRODUCTS_AND_SERVICES_ADS"] = "ALL",
    date_min: Date | None = None, date_max: Date | None = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
) -> dict[str, Any]:
    """Search Meta's Ad Library (DSA Art. 39 repository for EU ads)."""
    return await service().ad_search(countries=countries, search_terms=search_terms, page_ids=page_ids,
                                     ad_type=ad_type, date_min=date_min, date_max=date_max, limit=limit)


@mcp.tool()
@tool_errors
async def ad_advertiser_summary(page_id: str, countries: list[str], date_min: Date | None = None,
                                date_max: Date | None = None) -> dict[str, Any]:
    """Summarise one advertiser page's ads: volume, dates, platforms, beneficiaries/payers, EU reach."""
    return await service().advertiser_summary(page_id=page_id, countries=countries, date_min=date_min,
                                              date_max=date_max)


@mcp.resource("dsalens://evidence/{evidence_id}")
def evidence_resource(evidence_id: str) -> str:
    """Evidence record behind a statistic."""
    return json.dumps(service().rt.evidence.get(evidence_id) or {"error": "unknown evidence id"}, indent=1)


def main(argv: list[str] | None = None) -> None:
    configure_logging()
    parser = argparse.ArgumentParser(prog="agent-osint-dsalens")
    sub = parser.add_subparsers(dest="cmd")
    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    p_ing = sub.add_parser("ingest", help="ingest daily dump zip/csv files")
    p_ing.add_argument("files", nargs="+")
    p_ing.add_argument("--platform", action="append")
    args = parser.parse_args(argv)
    if args.cmd == "ingest":
        for f in args.files:
            print(json.dumps(service().ingest_dump(f, platforms=args.platform)), flush=True)
        return
    mcp.run(transport=getattr(args, "transport", "stdio"))


if __name__ == "__main__":
    main()
