"""Protocol-level tests: every server is started as a real MCP stdio subprocess, tools are listed with
valid JSON schemas, and representative tools are called through the MCP client."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_osint.core.http import write_fixture
from tests.test_sitegraph import _serve_network

EXPECTED_TOOLS = {
    "sitegraph": {"fingerprint_site", "attribute_operator", "find_siblings", "compare_sites", "explain_link",
                  "index_domains", "site_history", "index_stats"},
    "commentforensics": {"docket_overview", "ingest_docket", "ingest_bulk_csv", "docket_summary", "campaign_clusters",
                         "cluster_detail", "campaign_sponsor_hints", "search_comments"},
    "integritylens": {"integrity_report", "screen_references", "journal_legitimacy", "scan_tortured_phrases",
                      "load_dataset", "sources_status"},
    "dsalens": {"moderation_stats", "compare_platforms", "count_statements", "sample_statements", "ingest_dump",
                "dataset_status", "ad_library_search", "ad_advertiser_summary"},
    "procurement": {"load_ocds", "fetch_uk_awards", "red_flags", "buyer_profile", "supplier_profile", "check_supplier",
                    "enrich_suppliers", "load_sanctions_file", "ruleset"},
    "depcheck": {"check_packages", "check_imports", "check_manifest", "suggest_package"},
    "claimtrail": {"claim_timeline", "url_timeline", "fact_checks", "claimreview_from_url", "image_provenance"},
}


class _R:
    def __init__(self, d: Path):
        self.dir = d

    def add(self, url, body=b"", *, status=200, method="GET", headers=None, json_body=None, final_url=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
            headers = {"content-type": "application/json", **(headers or {})}
        write_fixture(self.dir, method, url, status=status, body=body, headers=headers, json_body=json_body,
                      final_url=final_url)


def _params(server: str, tmp: Path) -> StdioServerParameters:
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY") and not k.endswith("_TOKEN")}
    env.update({"AGENT_OSINT_HOME": str(tmp / "home"), "AGENT_OSINT_REPLAY_DIR": str(tmp / "replay"),
                "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
    return StdioServerParameters(command=sys.executable, args=["-m", f"agent_osint.{server}.server"], env=env)


async def _call(session: ClientSession, name: str, args: dict) -> dict:
    res = await session.call_tool(name, args)
    assert not res.isError, res.content
    if res.structuredContent is not None:
        sc = res.structuredContent
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(res.content[0].text)


@pytest.mark.parametrize("server", sorted(EXPECTED_TOOLS))
async def test_server_lists_tools(server, tmp_path):
    (tmp_path / "replay").mkdir()
    async with stdio_client(_params(server, tmp_path)) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            assert init.serverInfo.name == server
            assert init.instructions
            tools = await s.list_tools()
            names = {t.name for t in tools.tools}
            assert names == EXPECTED_TOOLS[server]
            for t in tools.tools:
                assert t.description and t.inputSchema.get("type") == "object"


async def test_sitegraph_over_stdio(tmp_path):
    rdir = tmp_path / "replay"
    rdir.mkdir()
    _serve_network(_R(rdir))
    async with stdio_client(_params("sitegraph", tmp_path)) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            att = await _call(s, "attribute_operator", {"domain": "alpha-daily-news.com"})
            assert att["ok"] and "example-media.com" in att["data"]["candidates"][0]["domains"]
            await _call(s, "fingerprint_site", {"domain": "beta-daily-news.com"})
            sib = await _call(s, "find_siblings", {"query": "alpha-daily-news.com"})
            assert sib["data"]["siblings"][0]["domain"] == "beta-daily-news.com"
            ev_id = att["evidence"][0]["id"]
            res = await s.read_resource(f"sitegraph://evidence/{ev_id}")
            ev = json.loads(res.contents[0].text)
            assert ev["id"] == ev_id and ev["sha256"]
            bad = await _call(s, "fingerprint_site", {"domain": "not a domain!!"})
            assert bad["ok"] is False and bad["error"]["code"] == "invalid_input"


async def test_depcheck_and_integritylens_over_stdio(tmp_path):
    (tmp_path / "replay").mkdir()
    async with stdio_client(_params("integritylens", tmp_path)) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await _call(s, "scan_tortured_phrases", {"text": "We used counterfeit consciousness and a "
                                                                    "profound neural organization for colossal information."})
            assert res["data"]["distinct_phrases"] == 3
    async with stdio_client(_params("depcheck", tmp_path)) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await _call(s, "check_imports", {"code": "import os\n", "language": "python"})
            assert res["ok"] and res["data"]["dependencies"] == []
