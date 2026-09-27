# Tool reference

Generated from the servers' MCP tool schemas (`python scripts/gen_tools_doc.py`).

## sitegraph

_Command:_ `agent-osint-sitegraph`

> SiteGraph attributes websites to the organisations that operate or monetise them and finds sibling sites sharing operational fingerprints (ads.txt/sellers.json accounts, analytics/tag IDs, legal-notice registry numbers, security.txt, certificates). Every link carries evidence IDs; read them via the sitegraph://evidence/{id} resource. Scores are tiers (strong/likely/possible), never proof. Names of natural persons are always withheld. Start with attribute_operator or fingerprint_site; use find_siblings to pivot.

### `fingerprint_site`

Collect every operator fingerprint for a site: tracker/ad IDs, ads.txt + sellers.json records, security.txt, legal-notice entities and registry numbers, schema.org organisation, certificates.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `domain` | string | yes |  | Domain or URL, e.g. 'example.com' |
| `include_certificates` | boolean |  | true | Query Certificate Transparency for co-certified domains |
| `refresh` | boolean |  | false | Ignore the 7-day fingerprint cache |

### `attribute_operator`

Rank candidate organisations that operate/own/monetise a site, with confidence and per-source evidence.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `domain` | string | yes |  | Domain or URL |
| `refresh` | boolean |  | false | Ignore cached fingerprint |

### `find_siblings`

Find other domains in the index that share identifiers with a domain (or carry a given identifier).

| parameter | type | required | default | description |
|---|---|---|---|---|
| `query` | string | yes |  | A domain, or an identifier such as 'UA-12345', 'G-ABC123XYZ9', 'GTM-ABC123', 'pub-1234567890123456', or 'adsystem.com, ACCOUNTID' |
| `limit` | integer |  | 25 |  |
| `min_score` | number |  | 0.0 |  |

### `compare_sites`

Pairwise shared-fingerprint matrix with a scored verdict for each pair.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `domains` | array | yes |  | 2-10 domains |
| `refresh` | boolean |  | false |  |

### `explain_link`

Human-readable evidence chain for why two sites are (or are not) linked.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `a` | string | yes |  |  |
| `b` | string | yes |  |  |
| `refresh` | boolean |  | false |  |

### `index_domains`

Fingerprint a batch of domains so later sibling searches can find them.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `domains` | array | yes |  | Up to 25 domains to fingerprint into the index |
| `include_certificates` | boolean |  | false |  |

### `site_history`

First/latest Wayback Machine capture and years with captures (a proxy for site age).

| parameter | type | required | default | description |
|---|---|---|---|---|
| `domain` | string | yes |  |  |

### `index_stats`

Size and composition of the local identifier index.

**Resource:** `sitegraph://evidence/{evidence_id}` — Raw evidence record (URL, fetch time, SHA-256, stored bytes) behind a claim.

## commentforensics

_Command:_ `agent-osint-commentforensics`

> CommentForensics analyses public comments on Regulations.gov dockets. Workflow: docket_overview -> ingest_docket (call repeatedly until complete; progress is saved) -> docket_summary / campaign_clusters -> cluster_detail / campaign_sponsor_hints. Clusters are groups of near-identical text (form letters). Coordination is lawful and common: describe clusters as coordinated campaigns, not fraud. Commenter names are never returned.

### `docket_overview`

Docket title/agency, its documents and reported comment totals, plus local ingestion status.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `docket_id` | string | yes |  | Regulations.gov docket ID, e.g. 'EPA-HQ-OAR-2021-0317' |

### `ingest_docket`

Download comments for a docket incrementally (resumable). Call again until 'complete' is true.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `docket_id` | string | yes |  | Regulations.gov docket ID, e.g. 'EPA-HQ-OAR-2021-0317' |
| `source` | string (auto, api, mirrulations) |  | "auto" | 'api' needs REGULATIONS_GOV_API_KEY; 'mirrulations' uses the public S3 mirror |
| `max_requests` | integer |  | 80 | HTTP request budget for this call |

### `ingest_bulk_csv`

Load comments from a Regulations.gov bulk-export CSV (fastest route for very large dockets).

| parameter | type | required | default | description |
|---|---|---|---|---|
| `path` | string | yes |  | Path (on the server machine) to a Regulations.gov bulk-download CSV |
| `docket_id` | string \| null |  | null | Override docket ID if the CSV lacks one |

### `docket_summary`

Totals, unique texts, share of comments in campaigns, largest campaigns, top organisations, daily volume.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `docket_id` | string | yes |  | Regulations.gov docket ID, e.g. 'EPA-HQ-OAR-2021-0317' |
| `threshold` | number |  | 0.8 | Jaccard similarity threshold for grouping |
| `min_size` | integer |  | 5 |  |

### `campaign_clusters`

Near-duplicate comment clusters (form-letter campaigns) with template text and timing.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `docket_id` | string | yes |  | Regulations.gov docket ID, e.g. 'EPA-HQ-OAR-2021-0317' |
| `threshold` | number |  | 0.8 | Jaccard similarity threshold for grouping |
| `min_size` | integer |  | 5 |  |
| `limit` | integer |  | 20 |  |

### `cluster_detail`

One campaign: template text, sample comment links, organisations, posting timeline.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `docket_id` | string | yes |  | Regulations.gov docket ID, e.g. 'EPA-HQ-OAR-2021-0317' |
| `cluster_id` | string | yes |  |  |
| `threshold` | number |  | 0.8 | Jaccard similarity threshold for grouping |
| `sample` | integer |  | 10 |  |

### `campaign_sponsor_hints`

Leads on who organised a campaign: organisation fields, domains, platform footers, affiliation phrases.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `docket_id` | string | yes |  | Regulations.gov docket ID, e.g. 'EPA-HQ-OAR-2021-0317' |
| `cluster_id` | string | yes |  |  |
| `threshold` | number |  | 0.8 | Jaccard similarity threshold for grouping |

### `search_comments`

Find stored comments containing a phrase (e.g. to trace a template sentence).

| parameter | type | required | default | description |
|---|---|---|---|---|
| `docket_id` | string | yes |  | Regulations.gov docket ID, e.g. 'EPA-HQ-OAR-2021-0317' |
| `phrase` | string | yes |  |  |
| `limit` | integer |  | 20 |  |

**Resource:** `commentforensics://evidence/{evidence_id}` — Evidence record (URL, fetch time, SHA-256) behind stored comments.

## integritylens

_Command:_ `agent-osint-integritylens`

> IntegrityLens checks scholarly works and journals for integrity signals: retractions, expressions of concern and corrections (Crossref incl. Retraction Watch data), OpenAlex retraction flags, unregistered DOIs (possible fabricated citations), PubPeer discussion counts, Problematic Paper Screener flags and hijacked-journal lists (user-loaded), and tortured phrases. Report signals with their sources; never call a paper 'clean' — say 'no signals in the sources checked'. Never characterise individuals.

### `integrity_report`

All integrity signals for one work, each with its source, date and evidence.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `doi` | string | yes |  | DOI or doi.org URL |

### `screen_references`

Screen every DOI in a reference list; flags retractions, concerns and DOIs that do not exist.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `text` | string \| null |  | null | Reference list / BibTeX / any text containing DOIs |
| `dois` | array \| null |  | null | Explicit DOIs |
| `max_items` | integer |  | 100 |  |

### `journal_legitimacy`

Hijacked-journal list matches, OpenAlex/DOAJ status, homepage mismatch, retraction counts.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `query` | string | yes |  | ISSN (e.g. 1234-5678), journal website URL, or journal title |

### `scan_tortured_phrases`

Detect 'tortured phrases' (e.g. 'counterfeit consciousness' for 'artificial intelligence').

| parameter | type | required | default | description |
|---|---|---|---|---|
| `text` | string | yes |  | Abstract or full text |

### `load_dataset`

Load a locally downloaded dataset (Retraction Watch CSV, PPS export, hijacked-journal list).

| parameter | type | required | default | description |
|---|---|---|---|---|
| `kind` | string (retraction_watch, problematic_paper_screener, hijacked_journals) | yes |  |  |
| `path` | string | yes |  | CSV path on the server machine |

### `sources_status`

Which sources are enabled / loaded.

**Resource:** `integritylens://evidence/{evidence_id}` — Evidence record behind a signal.

## dsalens

_Command:_ `agent-osint-dsalens`

> DSA-Lens answers questions about platform content moderation in the EU using the DSA Transparency Database (statements of reasons) via the Research API (last 6 months, needs DSA_TDB_TOKEN) or locally ingested daily dumps (any period), plus Meta's Ad Library (needs META_AD_LIBRARY_TOKEN). Numbers are platform self-reports; say so when comparing platforms.

### `moderation_stats`

Counts of statements of reasons grouped by a field (category, decision ground, automation, ...).

| parameter | type | required | default | description |
|---|---|---|---|---|
| `group_by` | string | yes |  | One of ['account_type', 'automated_decision', 'automated_detection', 'category', 'category_specification', 'content_language', 'content_type', 'decision_account', 'decision_ground', 'decision_monetary', 'decision_provision', 'decision_visibility', 'incompatible_content_illegal', 'platform_name', 'received_date', 'source_type', 'territorial_scope'] |
| `date_from` | string | yes |  | YYYY-MM-DD |
| `date_to` | string | yes |  | YYYY-MM-DD |
| `platform` | string \| null |  | null | Platform name (e.g. 'TikTok') or numeric platform_id |
| `country` | string \| null |  | null | ISO country in territorial_scope (local source only) |
| `source` | string (auto, api, local) |  | "auto" | 'api' = Research API, 'local' = ingested dumps |
| `limit` | integer |  | 30 |  |

### `compare_platforms`

Per-platform volume, share of fully automated decisions, automated detection, illegal-content ground, top categories.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `date_from` | string | yes |  | YYYY-MM-DD |
| `date_to` | string | yes |  | YYYY-MM-DD |
| `platforms` | array \| null |  | null | Platform names to include |
| `source` | string (auto, api, local) |  | "auto" | 'api' = Research API, 'local' = ingested dumps |
| `top` | integer |  | 15 |  |

### `count_statements`

Exact count via the Research API /count endpoint (requires DSA_TDB_TOKEN).

| parameter | type | required | default | description |
|---|---|---|---|---|
| `date_from` | string \| null |  | null |  |
| `date_to` | string \| null |  | null |  |
| `platform_id` | integer \| null |  | null |  |
| `filters` | object \| null |  | null | Exact-match terms, e.g. {'category': 'STATEMENT_CATEGORY_SCAMS_AND_FRAUD'} |

### `sample_statements`

A sample of individual statements of reasons (UUIDs and key fields) for qualitative review.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `n` | integer |  | 10 |  |
| `date_from` | string \| null |  | null |  |
| `date_to` | string \| null |  | null |  |
| `platform` | string \| null |  | null |  |
| `platform_id` | integer \| null |  | null |  |
| `country` | string \| null |  | null |  |
| `filters` | object \| null |  | null |  |
| `source` | string (auto, api, local) |  | "auto" | 'api' = Research API, 'local' = ingested dumps |

### `ingest_dump`

Load a DSA Transparency Database daily dump into the local analytical store.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `path` | string | yes |  | Local path to a daily dump .zip (or extracted .csv) |
| `platforms` | array \| null |  | null | Only keep these platform names |
| `max_rows` | integer \| null |  | null |  |

### `dataset_status`

Configured credentials and local data coverage.

### `ad_library_search`

Search Meta's Ad Library (DSA Art. 39 repository for EU ads).

| parameter | type | required | default | description |
|---|---|---|---|---|
| `countries` | array | yes |  | ISO codes, e.g. ['DE','FR'] |
| `search_terms` | string \| null |  | null |  |
| `page_ids` | array \| null |  | null |  |
| `ad_type` | string (ALL, POLITICAL_AND_ISSUE_ADS, EMPLOYMENT_ADS, HOUSING_ADS, FINANCIAL_PRODUCTS_AND_SERVICES_ADS) |  | "ALL" |  |
| `date_min` | string \| null |  | null |  |
| `date_max` | string \| null |  | null |  |
| `limit` | integer |  | 50 |  |

### `ad_advertiser_summary`

Summarise one advertiser page's ads: volume, dates, platforms, beneficiaries/payers, EU reach.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `page_id` | string | yes |  |  |
| `countries` | array | yes |  |  |
| `date_min` | string \| null |  | null |  |
| `date_max` | string \| null |  | null |  |

**Resource:** `dsalens://evidence/{evidence_id}` — Evidence record behind a statistic.

## procurement

_Command:_ `agent-osint-procurement`

> Procurement Red Flags loads public contracting data (OCDS: UK Find a Tender or any OCDS publisher), computes integrity risk indicators (single bidder, non-open procedure, short tender, estimate overruns, supplier concentration, shared addresses, split purchases, new or sanctioned suppliers) and profiles buyers/suppliers. Flags are indicators for review — never state or imply corruption.

### `load_ocds`

Load OCDS contracting data from any publisher.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `path_or_url` | string | yes |  | Local path or URL of an OCDS release/record package (JSON or JSON Lines) |

### `fetch_uk_awards`

Pull award-stage notices from UK Find a Tender's OCDS API (100 per page).

| parameter | type | required | default | description |
|---|---|---|---|---|
| `updated_from` | string | yes |  | YYYY-MM-DDTHH:MM:SS |
| `updated_to` | string \| null |  | null |  |
| `max_pages` | integer |  | 5 |  |

### `red_flags`

Awards with red flags, most severe first.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `buyer` | string \| null |  | null |  |
| `supplier` | string \| null |  | null |  |
| `rule` | string \| null |  | null | Filter to one rule id (see ruleset) |
| `min_severity` | string (low, medium, high, critical) |  | "low" |  |
| `limit` | integer |  | 50 |  |

### `buyer_profile`

A buyer's awards, procedures, single-bid rate, supplier concentration (HHI) and flags.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `buyer` | string | yes |  | Buyer id or name fragment |

### `supplier_profile`

A supplier's awards, buyers, flagged awards and registry/sanctions check results.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `supplier` | string | yes |  | Supplier id, identifier or name fragment |

### `check_supplier`

Registry (Companies House / GLEIF) and sanctions (OpenSanctions) check for one organisation.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `name` | string | yes |  |  |
| `country` | string \| null |  | null | ISO2 country |
| `company_number` | string \| null |  | null | UK Companies House number |

### `enrich_suppliers`

Run registry + sanctions checks for loaded suppliers so new_supplier / sanctions_match flags apply.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `max_suppliers` | integer |  | 20 |  |

### `load_sanctions_file`

Load an OpenSanctions bulk CSV for offline screening (entity schemas only).

| parameter | type | required | default | description |
|---|---|---|---|---|
| `path` | string | yes |  | OpenSanctions targets.simple.csv path |

### `ruleset`

Show (and optionally adjust) the red-flag rules and thresholds.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `short_tender_days` | integer \| null |  | null |  |
| `concentration_share` | number \| null |  | null |  |
| `split_threshold` | number \| null |  | null |  |
| `new_supplier_days` | integer \| null |  | null |  |

**Resource:** `procurement://evidence/{evidence_id}` — Evidence record for loaded data.

## depcheck

_Command:_ `agent-osint-depcheck`

> DepCheck verifies packages before installation: existence on PyPI/npm, age, release history, look-alikes of popular packages (typosquats), names previously hallucinated by LLMs (slopsquatting), install scripts, deprecation. Call check_imports on generated code or check_manifest on requirements/package.json BEFORE running pip/npm install. Never install a package with verdict 'block'.

### `check_packages`

Verdict (ok / caution / block) and risk factors for each package name.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `ecosystem` | string (pypi, npm) | yes |  |  |
| `names` | array | yes |  |  |

### `check_imports`

Find third-party imports in code, map them to package names (e.g. cv2 -> opencv-python) and check them.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `code` | string | yes |  | Source code |
| `language` | string (python, javascript, typescript) | yes |  |  |
| `local_modules` | array \| null |  | null | Project-local module names to ignore |

### `check_manifest`

Check every dependency declared in requirements.txt, pyproject.toml or package.json content.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `content` | string | yes |  |  |
| `kind` | string (requirements, pyproject, package_json) | yes |  |  |

### `suggest_package`

Popular real packages with names close to the given one ("did you mean").

| parameter | type | required | default | description |
|---|---|---|---|---|
| `ecosystem` | string (pypi, npm) | yes |  |  |
| `name` | string | yes |  |  |

## claimtrail

_Command:_ `agent-osint-claimtrail`

> ClaimTrail builds provenance timelines: earliest Internet Archive captures, page-declared publish dates (live and in the earliest archived copy), fact-checks (Google Fact Check Tools / ClaimReview markup) and C2PA Content Credentials for images. 'Earliest known' is bounded by coverage; say so.

### `claim_timeline`

Merged, dated timeline for a claim across URLs, archives and fact-checks.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `claim` | string \| null |  | null | Claim text to look up in fact-checks |
| `urls` | array \| null |  | null | Up to 10 URLs where it appeared |
| `language` | string \| null |  | null |  |

### `url_timeline`

First/latest archive capture, declared publish dates (live + earliest archived copy), removal status.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `url` | string | yes |  |  |

### `fact_checks`

Search published fact-checks (Google Fact Check Tools API; needs GOOGLE_FACTCHECK_API_KEY).

| parameter | type | required | default | description |
|---|---|---|---|---|
| `query` | string | yes |  |  |
| `language` | string \| null |  | null |  |
| `max_age_days` | integer \| null |  | null |  |

### `claimreview_from_url`

Extract schema.org ClaimReview data (claim, rating, dates, appearances) from a fact-check article.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `url` | string | yes |  |  |

### `image_provenance`

Read C2PA Content Credentials: signer, time, actions, AI-generation declarations, validation state.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `source` | string | yes |  | Image/video URL or local path |

**Resource:** `claimtrail://evidence/{evidence_id}` — Evidence record behind a timeline event.

