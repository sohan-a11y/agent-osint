"""Regenerate docs/TOOLS.md from the servers' live MCP tool schemas.

    python scripts/gen_tools_doc.py > docs/TOOLS.md
"""

from __future__ import annotations

import asyncio
import importlib
import json

SERVERS = ["sitegraph", "commentforensics", "integritylens", "dsalens", "procurement", "depcheck", "claimtrail"]


def _type(p: dict) -> str:
    typ = p.get("type") or " \\| ".join(x.get("type", "?") for x in p.get("anyOf", []) if isinstance(x, dict))
    enums = p.get("enum") or next((x["enum"] for x in p.get("anyOf", []) if isinstance(x, dict) and "enum" in x), None)
    return f"{typ} ({', '.join(map(str, enums))})" if enums else typ


async def main() -> None:
    print("# Tool reference\n")
    print("Generated from the servers' MCP tool schemas (`python scripts/gen_tools_doc.py`).\n")
    for s in SERVERS:
        mcp = importlib.import_module(f"agent_osint.{s}.server").mcp
        print(f"## {s}\n\n_Command:_ `agent-osint-{s}`\n")
        print("> " + " ".join(mcp.instructions.split()) + "\n")
        for t in await mcp.list_tools():
            print(f"### `{t.name}`\n\n" + " ".join((t.description or "").split()) + "\n")
            props = t.inputSchema.get("properties", {})
            req = set(t.inputSchema.get("required", []))
            if props:
                print("| parameter | type | required | default | description |\n|---|---|---|---|---|")
                for name, p in props.items():
                    default = json.dumps(p["default"]) if "default" in p else ""
                    desc = (p.get("description") or "").replace("|", "\\|")
                    print(f"| `{name}` | {_type(p)} | {'yes' if name in req else ''} | {default} | {desc} |")
                print()
        for rt in await mcp.list_resource_templates():
            print(f"**Resource:** `{rt.uriTemplate}` — {rt.description}\n")


if __name__ == "__main__":
    asyncio.run(main())
