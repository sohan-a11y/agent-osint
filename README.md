# agent-osint

Seven organisation-focused OSINT servers for AI agents, speaking the [Model Context Protocol](https://modelcontextprotocol.io).
Attach them to Claude, Cursor, or any MCP client and your agent gains verifiable, evidence-backed answers to questions
LLMs cannot answer from memory.

| Server | Question it answers | Key sources | Needs a key? |
|---|---|---|---|
| **SiteGraph** | Who operates this website, and which other sites share its operator? | ads.txt / app-ads.txt, sellers.json, analytics & tag IDs, legal notices (Impressum), security.txt, Certificate Transparency, Wayback, Common Crawl | No (optional `CERTSPOTTER_API_KEY`) |
| **CommentForensics** | Which public comments on a US rulemaking are coordinated form-letter campaigns, and who is behind them? | Regulations.gov API v4, Mirrulations mirror, bulk CSV exports | Optional (`REGULATIONS_GOV_API_KEY`; mirror needs none) |
| **IntegrityLens** | Is this paper/reference list/journal trustworthy? | Crossref (incl. Retraction Watch data), OpenAlex, doi.org, PubPeer, Problematic Paper Screener & hijacked-journal lists (you load them), tortured-phrase detector | Optional (`PUBPEER_DEVKEY`) |
| **DSA-Lens** | How do platforms moderate content in the EU? What ads ran? | EU DSA Transparency Database (Research API + daily dumps), Meta Ad Library | `DSA_TDB_TOKEN` for API route; `META_AD_LIBRARY_TOKEN` for ads. Dumps need none |
| **Procurement Red Flags** | Which public contracts show integrity risk indicators? | OCDS data (UK Find a Tender or any publisher), GLEIF, Companies House, OpenSanctions | Optional (`COMPANIES_HOUSE_API_KEY`, `OPENSANCTIONS_API_KEY`) |
| **DepCheck** | Is the package my coding agent wants to install real and safe? | PyPI, npm, popularity lists, Trend Micro slopsquatting dataset | No |
| **ClaimTrail** | When and where did this claim/page/image first appear? Has it been fact-checked? | Wayback Machine, page metadata, ClaimReview, Google Fact Check Tools, C2PA | Optional (`GOOGLE_FACTCHECK_API_KEY`) |

48 tools in total — see [docs/TOOLS.md](docs/TOOLS.md). Verification status is documented honestly in [VERIFICATION.md](VERIFICATION.md).

## Design principles

* **Evidence on every claim.** Each finding carries evidence IDs (URL, fetch time, HTTP status, SHA-256 of the raw
  bytes, and the bytes where lawful to keep). Read them via the `<server>://evidence/{id}` MCP resource.
* **Uniform envelope.** Every tool returns `{"ok", "data", "evidence", "warnings", "limitations", "sources_checked"}`
  or `{"ok": false, "error": {"code", "message", "hint"}}` — agents never see stack traces.
* **Organisations, not people.** Names that look like private individuals (sole traders, commenters, signatories,
  PubPeer users, ad payers) are withheld; personal emails and phone numbers are masked. There is no switch to disable this.
* **Polite by default.** Conservative per-host token-bucket rate limits (matching providers' published limits where
  they publish them), retries with
  `Retry-After`, robots.txt (RFC 9309) for HTML pages, response-size caps, and a local response cache.
* **Safe to expose to an agent.** Requests to loopback/private/link-local addresses are refused (SSRF guard), local
  file inputs can be confined with `AGENT_OSINT_FILE_ROOTS`, and file types are checked.
* **Honest outputs.** Scores are tiers with stated rules, "no signal" is never presented as "clean", and every tool
  lists its limitations.

## Install

Requires Python 3.10+.

```bash
pip install ./agent-osint            # from this folder
pip install "./agent-osint[c2pa]"    # + C2PA reading for ClaimTrail
```

Or run without installing, with [uv](https://docs.astral.sh/uv/): `uvx --from ./agent-osint agent-osint-depcheck`.

Docker: `docker build -t agent-osint .` then `docker run -i --rm agent-osint agent-osint-depcheck`.

## Connect to your AI

**Claude Desktop** — edit `claude_desktop_config.json` (Windows: `%APPDATA%\Claude\`, macOS:
`~/Library/Application Support/Claude/`):

```json
{
  "mcpServers": {
    "depcheck":        { "command": "agent-osint-depcheck" },
    "sitegraph":       { "command": "agent-osint-sitegraph", "env": { "AGENT_OSINT_CONTACT": "you@example.org" } },
    "integritylens":   { "command": "agent-osint-integritylens", "env": { "AGENT_OSINT_CONTACT": "you@example.org" } },
    "commentforensics":{ "command": "agent-osint-commentforensics", "env": { "REGULATIONS_GOV_API_KEY": "..." } },
    "dsalens":         { "command": "agent-osint-dsalens", "env": { "DSA_TDB_TOKEN": "...", "META_AD_LIBRARY_TOKEN": "..." } },
    "procurement":     { "command": "agent-osint-procurement", "env": { "COMPANIES_HOUSE_API_KEY": "..." } },
    "claimtrail":      { "command": "agent-osint-claimtrail", "env": { "GOOGLE_FACTCHECK_API_KEY": "..." } }
  }
}
```

**Claude Code** — `claude mcp add depcheck -- agent-osint-depcheck` (repeat per server; add `-e KEY=value` for env).

**Cursor / other clients** — any MCP client that launches stdio servers works with the same commands. For remote use,
run `agent-osint-<server> serve --transport streamable-http`.

## Configuration

| Variable | Purpose |
|---|---|
| `AGENT_OSINT_HOME` | Data directory (default `~/.agent-osint`): caches, indexes, evidence |
| `AGENT_OSINT_CONTACT` | Your email, sent in the User-Agent and to Crossref/OpenAlex "polite pools" (recommended) |
| `AGENT_OSINT_FILE_ROOTS` | `os.pathsep`-separated folders that local-file tools may read (recommended when exposing to agents) |
| `AGENT_OSINT_ALLOW_PRIVATE_NETWORK` | `1` to allow fetching private/loopback addresses (off by default) |
| `AGENT_OSINT_HOST_RPS` | Override rate limits, e.g. `api.crossref.org=10,pypi.org=5` |
| `AGENT_OSINT_CACHE_TTL` / `AGENT_OSINT_HTTP_TIMEOUT` / `AGENT_OSINT_MAX_BYTES` | Cache lifetime (s), timeout (s), response cap (bytes) |
| `AGENT_OSINT_RECORD_DIR` / `AGENT_OSINT_REPLAY_DIR` | Record live responses as fixtures / replay them offline |
| `AGENT_OSINT_TORTURED_PHRASES` | Extra tortured-phrase list (`tortured => expected` per line) |
| `REGULATIONS_GOV_API_KEY` | Free key from [api.data.gov](https://open.gsa.gov/api/regulationsgov/) |
| `DSA_TDB_TOKEN` | DSA Transparency Database Research API token (EU Login + request to the DSA helpdesk) |
| `META_AD_LIBRARY_TOKEN` / `META_GRAPH_VERSION` | Meta Ad Library access token / Graph API version (default `v26.0`) |
| `PUBPEER_DEVKEY` | PubPeer API key (request from PubPeer) |
| `GOOGLE_FACTCHECK_API_KEY` | Google Cloud key with the Fact Check Tools API enabled |
| `COMPANIES_HOUSE_API_KEY` | UK Companies House REST API key |
| `OPENSANCTIONS_API_KEY` / `OPENSANCTIONS_URL` | OpenSanctions hosted API key, or the URL of your self-hosted yente |
| `CERTSPOTTER_API_KEY` | SSLMate Cert Spotter key for higher CT query limits |

A template is in [.env.example](.env.example).

## Command-line utilities

```bash
agent-osint-depcheck check requirements.txt          # CI gate: exit 1 if any dependency is 'block'
agent-osint-sitegraph index domains.txt              # bulk-fingerprint a domain list into the index
agent-osint-sitegraph ingest-warc CC-MAIN-*.warc.gz  # seed the sibling index from Common Crawl WARC files
agent-osint-commentforensics ingest EPA-HQ-OAR-2021-0317 --source mirrulations
agent-osint-commentforensics ingest-csv export.csv   # Regulations.gov bulk download
agent-osint-dsalens ingest sor-global-2026-09-01-light.zip --platform TikTok
agent-osint-integritylens load retraction_watch retraction_watch.csv
agent-osint-procurement load https://example.org/ocds/release-package.json
python scripts/live_smoke.py                          # live end-to-end check of every server
python scripts/slopsquat_benchmark.py                 # re-check LLM-hallucinated package names on PyPI
```

## Development

```bash
pip install -e ".[dev,c2pa]"
pytest                 # offline suite (replay fixtures, real MCP stdio sessions)
pytest -m live         # live registry tests (needs internet)
ruff check src tests
```

## Responsible use

These tools surface public information about organisations. Shared identifiers, form-letter campaigns and
procurement red flags are **indicators for further review**, not proof of ownership, fraud or corruption. Verify
before you publish, give organisations a chance to respond, and respect each data source's terms of use (see
[NOTICE](NOTICE) and [VERIFICATION.md](VERIFICATION.md)).

## License

Apache License 2.0. Third-party data and test fixtures are listed in [NOTICE](NOTICE).
