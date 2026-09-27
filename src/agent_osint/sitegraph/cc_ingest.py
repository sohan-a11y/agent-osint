"""Seed the SiteGraph index from Common Crawl WARC files.

Usage (CLI): ``agent-osint-sitegraph ingest-warc <file-or-url> [...]``

Each HTML response record is parsed with the same extractor used for live
fingerprints; identifiers are stored with source ``commoncrawl`` and evidence
pointing at the WARC file + record ID. Only the first ``max_pages_per_host``
pages per host are processed to keep index size bounded.

Common Crawl data is subject to its Terms of Use (https://commoncrawl.org/terms-of-use);
the index stores derived identifiers, not page content.
"""

from __future__ import annotations

import gzip
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

from warcio.archiveiterator import ArchiveIterator

from ..core.domains import registrable_domain
from ..core.evidence import EvidenceStore
from .extract import extract_page, normalise_org_name
from .store import SignalStore

SOURCE = "commoncrawl"


def _iter_html_records(path: Path) -> Iterator[tuple[str, str, str, bytes]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as fh:  # type: ignore[operator]
        for record in ArchiveIterator(fh):
            if record.rec_type != "response":
                continue
            url = record.rec_headers.get_header("WARC-Target-URI") or ""
            rid = record.rec_headers.get_header("WARC-Record-ID") or ""
            date = record.rec_headers.get_header("WARC-Date") or ""
            ctype = (record.http_headers.get_header("Content-Type") if record.http_headers else "") or ""
            if "html" not in ctype.lower():
                continue
            status = record.http_headers.get_statuscode() if record.http_headers else None
            if status and not str(status).startswith("2"):
                continue
            body = record.content_stream().read(3 * 1024 * 1024)
            yield url, rid, date, body


def ingest_warc(path: Path, store: SignalStore, evidence: EvidenceStore, *, max_pages_per_host: int = 3,
                max_records: int | None = None) -> dict[str, int]:
    seen_per_host: Counter[str] = Counter()
    stats = Counter()
    for url, rid, date, body in _iter_html_records(path):
        stats["html_records"] += 1
        if max_records and stats["html_records"] > max_records:
            break
        host = (url.split("/")[2] if "//" in url else "").lower().split(":")[0]
        if not host or not registrable_domain(host):
            continue
        if seen_per_host[host] >= max_pages_per_host:
            continue
        seen_per_host[host] += 1
        try:
            page = extract_page(body.decode("utf-8", errors="replace"), url)
        except Exception:  # noqa: BLE001 - malformed HTML is common in crawls
            stats["parse_errors"] += 1
            continue
        sigs: list[tuple[str, str, str | None]] = []
        for kind, values in page.ids.items():
            sigs += [(kind, v, None) for v in values]
            if kind == "adsense":
                sigs += [("google_pub", v, None) for v in values]
        sigs += [("fb_domain_verification", v, None) for v in page.fb_domain_verification]
        for org in page.jsonld_orgs[:3]:
            sigs.append(("jsonld_org", normalise_org_name(org["name"]), None))
        if not sigs:
            continue
        ev = evidence.record_derived(
            url, note=f"Common Crawl WARC {path.name} record {rid} captured {date}",
            excerpt=", ".join(sorted({f"{k}={v}" for k, v, _ in sigs}))[:480],
        )
        stats["signals"] += store.add_signals(host, [(k, v, ev.id) for k, v, _ in sigs], SOURCE)
        stats["hosts_with_signals"] = len(seen_per_host)
    return dict(stats)
