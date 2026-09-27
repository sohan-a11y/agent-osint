# Changelog

## 0.1.0 — 2026-09-27

First release: seven MCP servers, 48 tools.

* **SiteGraph** — operator attribution and sibling-site discovery from ads.txt/app-ads.txt (IAB 1.1),
  sellers.json (streamed index), tracker IDs, legal notices, security.txt, Certificate Transparency, schema.org,
  Wayback history; persistent identifier index; Common Crawl WARC ingestion.
* **CommentForensics** — resumable Regulations.gov ingestion (API v4 with >5,000-comment windowing, Mirrulations
  mirror, bulk CSV), MinHash-LSH campaign clustering, sponsor hints, phrase search.
* **IntegrityLens** — Crossref retraction/concern/correction signals incl. Retraction Watch backfill, OpenAlex,
  unregistered-DOI detection, PubPeer counts, user-loaded RW/PPS/hijacked-journal datasets, tortured-phrase scanner.
* **DSA-Lens** — DSA Transparency Database Research API (aggregates/count/search) and local dump ingestion into
  DuckDB; Meta Ad Library search and advertiser summaries.
* **Procurement Red Flags** — OCDS loading (any publisher; UK Find a Tender fetcher), 12 red-flag rules, buyer and
  supplier profiles, GLEIF/Companies House/OpenSanctions supplier checks.
* **DepCheck** — PyPI/npm existence, age, look-alike, slopsquatting-name, npm security-placeholder, install-script and
  deprecation checks for packages, imports and manifests; CI gate CLI.
* **ClaimTrail** — URL and claim timelines from Wayback captures, declared dates, ClaimReview and Google Fact Check
  Tools; C2PA Content Credentials reader.
* Shared core: evidence store, uniform result envelope, natural-person suppression, token-bucket rate limiting,
  RFC 9309 robots handling, response cache, record/replay fixtures, SSRF guard, file-root confinement.
