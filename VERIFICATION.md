# Verification report — agent-osint 0.1.0 (2026-09-27)

This document states exactly what was verified, how, and what was **not** verified. Read it before relying on any
server in production.

## Build environment and the one big constraint

Everything was built and tested in a cloud sandbox whose network allowlist permitted only PyPI, npm and GitHub
(raw files and `git clone`). All other data sources were unreachable from the sandbox: Regulations.gov,
Crossref, OpenAlex, doi.org, crt.sh, Cert Spotter, the Internet Archive, the DSA Transparency Database, Meta, UK
Find a Tender, GLEIF, Companies House, OpenSanctions, Google APIs, and arbitrary websites.
Consequences:

* **DepCheck is fully live-verified.** Its sources (PyPI, npm, GitHub raw) were reachable.
* **The other six servers are verified against recorded or specification-derived responses, not against the live
  services.** Where possible the fixtures are *real captured responses* or *official examples*, listed below.
  `scripts/live_smoke.py` performs the live verification; run it on a normal internet connection (see the last
  section).

## What was checked for every server

| Check | Result |
|---|---|
| Offline test suite (replay fixtures) | **66 passed**, 0 failed (`pytest -q`) |
| Same suite run from the **built wheel** in clean virtualenvs on Python **3.10, 3.11, 3.12, 3.13** | all passed (C2PA test auto-skips when the optional `c2pa` extra is absent; passes with it) |
| MCP protocol: each of the 7 servers launched as a real stdio subprocess, `initialize`, `list_tools` (48 tools, all with object input schemas), tool calls and evidence-resource reads through the official MCP client | passed (`tests/test_mcp_e2e.py`) |
| Edge-case fuzz: 47 malformed/edge inputs across all tools (empty/invalid domains, bad DOIs, missing keys, wrong file types, private-IP URLs, malformed JSON, reversed dates…) | **0 unhandled errors**: every call returned a structured `ok`/`error` envelope |
| Lint | `ruff check` clean |
| Privacy assertions | tests assert that planted personal names, personal emails, phone numbers, submitter name fields, PubPeer commenter names and sole-trader names never appear in outputs |
| Security | SSRF guard blocks loopback/private/link-local/metadata addresses (tested, incl. `169.254.169.254`); `AGENT_OSINT_FILE_ROOTS` confinement and path-traversal tested; file-type checks on local inputs |
| Packaging | wheel builds; all 7 console entry points start from a clean install |

A real-world compatibility bug was found this way: a clean install resolved **`mcp` 2.2.0, which renamed
`FastMCP`** and broke every server. The dependency is now pinned to `mcp>=1.9,<2` (clean installs resolve 1.30.0,
which passed the full suite). Porting to MCP SDK v2 is a follow-up task.

## Per-server status

Legend: ✅ verified against a primary source or real data · 🟡 implemented from documentation or memory,
tested only against synthetic fixtures · ❌ not verified.

### 1. SiteGraph
| Component | Status | Basis |
|---|---|---|
| ads.txt parsing incl. OWNERDOMAIN / MANAGERDOMAIN / redirect rules | ✅ | IAB Tech Lab ads.txt 1.1 spec (read) |
| sellers.json fields and streaming index | ✅ | IAB sellers.json 1.0 spec (read). Stress test: 300,000 sellers (38 MB) indexed in 1.3 s |
| Google sellers.json location `realtimebidding.google.com/sellers.json` | 🟡 | from memory |
| security.txt | ✅ | RFC 9116 |
| Tracker-ID extraction, legal-notice (Impressum) parsing, scoring | 🟡 | synthetic pages only; weights are documented expert priors, not learned |
| Certificate Transparency via Cert Spotter (`expand=dns_names`, `after` paging) | 🟡 | from memory; crt.sh was not used because its JSON may list only matching names (could not verify) |
| Wayback CDX | ✅ | Internet Archive CDX server README (read) |
| Common Crawl WARC ingestion | ✅ | tested with a real WARC written by `warcio` |
| Live run | ❌ | blocked in sandbox |

### 2. CommentForensics
| Component | Status | Basis |
|---|---|---|
| Regulations.gov v4 endpoints, paging (≤20 pages × 250), >5,000-comment `lastModifiedDate` windowing in **US Eastern time** | ✅ | GSA's published docs and OpenAPI spec (read); the Eastern-time conversion is tested against GSA's own worked example |
| Mirrulations S3 layout (`raw-data/<agency>/<docket>/text-<docket>/comments/`) and `(1)`-suffixed duplicate keys | ✅ | open-source code that used it on 2026-09-23 (read) |
| Bulk-export CSV columns | ✅ | same source |
| Clustering (MinHash-LSH on 5-word shingles) | ✅ | tests; benchmark: 50,000 comments clustered in 7.8 s |
| Sponsor/platform markers | 🟡 | heuristic phrase list |
| Live run | ❌ | blocked in sandbox |

### 3. IntegrityLens
| Component | Status | Basis |
|---|---|---|
| Crossref `update-to` / `updated-by` (Retraction Watch) analysis | ✅ | **real captured Crossref responses** (Wakefield 1998 retracted, Surgisphere 2020 retracted, Watson & Crick 1953 clean) |
| PubPeer v3 API (`POST /v3/publications?devkey=`, `{"dois": [...]}`, ≤40 per call) | ✅ | PubPeer's own Zotero plugin source (read) |
| Tortured phrases | ✅ | every pair from Table 1 of Cabanac et al. 2021 (arXiv:2107.06751) |
| OpenAlex fields (`is_retracted`, `primary_location.source.is_in_doaj`, `/sources/issn:`) and doi.org handle API | 🟡 | from memory |
| Retraction Watch CSV / PPS export / hijacked-journal list columns | 🟡 | loaded by flexible column matching; exact formats not verified |
| Live run | ❌ | blocked in sandbox |

### 4. DSA-Lens
| Component | Status | Basis |
|---|---|---|
| Research API endpoints, auth header, request/response shapes, limits, aggregatable fields | ✅ | official Research API page (read) |
| Statement-of-reasons attribute names and enum values | ✅ | official API documentation page (read) |
| Daily-dump **inner layout, CSV column names and array encoding** | 🟡 | not documented on the download page; the ingester walks nested zips, needs only `uuid` and `decision_ground`, and parses arrays tolerantly |
| Meta Ad Library query parameters | ✅ | Meta reference page (read) |
| Meta DSA fields (`eu_total_reach`, `beneficiary_payers`, …) | 🟡 | from memory; automatic fallback to base fields if the API rejects them (tested) |
| Live run | ❌ | needs `DSA_TDB_TOKEN` / `META_AD_LIBRARY_TOKEN` |

### 5. Procurement Red Flags
| Component | Status | Basis |
|---|---|---|
| OCDS parsing and release merging | ✅ | **official OCDS change-history example releases** (London Borough of Barnet) |
| UK Find a Tender endpoint and parameters | ✅ | FTS API documentation (read); the cursor field name was ambiguous, so both `cursor` and `links.next` are handled |
| GLEIF, Companies House, OpenSanctions/yente response shapes | 🟡 | from memory |
| Red-flag rules | ✅ logic / 🟡 thresholds | rules follow common procurement-integrity indicators; thresholds are tunable parameters, not legal standards |
| Live run | ❌ | blocked in sandbox |

### 6. DepCheck — live-verified
| Check (live, real registries) | Result |
|---|---|
| `requests`, `numpy`, `react`, `express`, `@types/node` | ok |
| `reqeusts` (typo) | block, "did you mean `requests`" |
| `crossenv` (the 2017 malicious npm typosquat of `cross-env`) | block: npm security placeholder |
| `lodahs` | block: npm security placeholder, look-alike of `lodash` |
| `expresss` | caution: placeholder version 0.0.0, look-alike of `express` |
| non-existent names | block |

**Slopsquatting benchmark** (`scripts/slopsquat_benchmark.py`, run 2026-09-27 against live PyPI): of the 126
package names that coding models hallucinated in Trend Micro's study, **25 names (22 distinct projects after PEP 503
normalisation) are now registered on PyPI**. 19 of the 22 projects were registered between 19 and 21 February 2026,
each with one release, and describe themselves as a "Benign slopsquatting research package". The other three are a
placeholder (`backpressure`), `TokenGuard` and `inferkit`. DepCheck returns "block" for 23 of the 25 registered names
and "caution" for 2 (both spellings of `TokenGuard`, which has 4 releases); the 101 names that are still unregistered
are all "block". This shows the rule firing on real registrations; it is **not** a claim that any of these packages is
malicious.

### 7. ClaimTrail
| Component | Status | Basis |
|---|---|---|
| Google Fact Check Tools `claims:search` parameters and response schema | ✅ | Google's published API discovery document (read) |
| Wayback CDX and raw archived copies (`id_`) | ✅ | Internet Archive CDX README |
| C2PA reading | ✅ | **real signed sample images** from the c2pa-python project: signed (valid, untrusted test certificate, `algorithmicMedia`), unsigned, and remote-manifest cases |
| Page-date and ClaimReview extraction | 🟡 | synthetic pages following schema.org |
| Live run | ❌ | blocked in sandbox |

## Not verified at all

* The Docker image was not built (no Docker in the sandbox).
* The GitHub Actions workflow has not been run.
* Nothing ran on Windows or macOS. The code avoids platform-specific paths and ships `tzdata` for Windows.
* No live runs with real API keys for Regulations.gov, DSA TDB, Meta, PubPeer, Google Fact Check, Companies House,
  OpenSanctions or Cert Spotter.
* No legal review of any data source's terms of use.

## How to finish verification (about 30 minutes plus key sign-ups)

```bash
pip install -e ".[dev,c2pa]"
export AGENT_OSINT_CONTACT=you@example.org
export AGENT_OSINT_RECORD_DIR=./recorded        # captures every real response as a replayable fixture
python scripts/live_smoke.py                    # key-free checks run; key-dependent ones are skipped and listed
# add keys one by one and re-run, e.g. REGULATIONS_GOV_API_KEY=... python scripts/live_smoke.py --only commentforensics
```

Each smoke check asserts stable real-world facts (for example, Wakefield 1998 is retracted and Watson & Crick 1953
is not), so a failure identifies a broken integration. Recorded fixtures can then replace synthetic ones in
`tests/`, which turns every 🟡 above into ✅.
