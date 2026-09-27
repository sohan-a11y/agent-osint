"""DepCheck MCP server: check dependencies before an agent installs them."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..core.results import tool_errors
from ..core.runtime import configure_logging, get_runtime
from .service import DepCheck

INSTRUCTIONS = """DepCheck verifies packages before installation: existence on PyPI/npm, age, release history,
look-alikes of popular packages (typosquats), names previously hallucinated by LLMs (slopsquatting), install
scripts, deprecation. Call check_imports on generated code or check_manifest on requirements/package.json
BEFORE running pip/npm install. Never install a package with verdict 'block'."""

mcp = FastMCP("depcheck", instructions=INSTRUCTIONS)
_service: DepCheck | None = None


def service() -> DepCheck:
    global _service
    if _service is None:
        _service = DepCheck(get_runtime("depcheck"))
    return _service


@mcp.tool()
@tool_errors
async def check_packages(ecosystem: Literal["pypi", "npm"],
                         names: Annotated[list[str], Field(min_length=1, max_length=100)]) -> dict[str, Any]:
    """Verdict (ok / caution / block) and risk factors for each package name."""
    return await service().check_packages(ecosystem, names)


@mcp.tool()
@tool_errors
async def check_imports(code: Annotated[str, Field(description="Source code")],
                        language: Literal["python", "javascript", "typescript"],
                        local_modules: Annotated[list[str] | None, Field(description="Project-local module names to ignore")] = None) -> dict[str, Any]:
    """Find third-party imports in code, map them to package names (e.g. cv2 -> opencv-python) and check them."""
    return await service().check_imports(code, language, local_modules)


@mcp.tool()
@tool_errors
async def check_manifest(content: str, kind: Literal["requirements", "pyproject", "package_json"]) -> dict[str, Any]:
    """Check every dependency declared in requirements.txt, pyproject.toml or package.json content."""
    return await service().check_manifest(content, kind)


@mcp.tool()
@tool_errors
async def suggest_package(ecosystem: Literal["pypi", "npm"], name: str) -> dict[str, Any]:
    """Popular real packages with names close to the given one ("did you mean")."""
    return await service().suggest(ecosystem, name)


def main(argv: list[str] | None = None) -> None:
    configure_logging()
    parser = argparse.ArgumentParser(prog="agent-osint-depcheck")
    sub = parser.add_subparsers(dest="cmd")
    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    p_check = sub.add_parser("check", help="check a manifest file; exit 1 if any package is blocked (CI use)")
    p_check.add_argument("file", type=Path)
    args = parser.parse_args(argv)
    if args.cmd == "check":
        name = args.file.name.lower()
        kind = "package_json" if name == "package.json" else "pyproject" if name == "pyproject.toml" else "requirements"
        res = asyncio.run(service().check_manifest(args.file.read_text(), kind))
        print(json.dumps(res, indent=1))
        blocked = res.get("ok") and res["data"]["summary"].get("block", 0)
        sys.exit(1 if blocked or not res.get("ok") else 0)
    mcp.run(transport=getattr(args, "transport", "stdio"))


if __name__ == "__main__":
    main()
