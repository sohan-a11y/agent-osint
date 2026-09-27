"""Comment data sources: Regulations.gov API v4, the Mirrulations S3 mirror, and bulk-export CSVs.

Regulations.gov v4 facts used here (from GSA's published documentation and OpenAPI spec):
* API key goes in the ``X-Api-Key`` header.
* ``/v4/documents?filter[docketId]=…`` lists a docket's documents; each has an ``objectId``.
* ``/v4/comments?filter[commentOnId]=<objectId>`` lists comments on a document; ``page[size]`` 5–250,
  ``page[number]`` 1–20, so one query sequence returns at most 5,000 comments.
* For more, sort by ``lastModifiedDate,documentId`` and re-query with
  ``filter[lastModifiedDate][ge]=<last value>``; that filter is expressed in **US Eastern time**
  as ``yyyy-MM-dd HH:mm:ss``.
* Full comment text (and ``docketId``) only comes from ``/v4/comments/{commentId}`` (one request each).
* The public submitter name fields are agency-configurable; this tool never stores them.

Mirrulations (https://github.com/mirrulations/mirrulations) mirrors raw v4 JSON on a public S3
bucket: ``raw-data/<agency>/<docket>/text-<docket>/comments/<commentId>.json``.
"""

from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..core.http import Fetcher, FetchError, FetchResult
from ..core.results import MissingCredential

API = "https://api.regulations.gov/v4"
MIRROR = "https://mirrulations.s3.amazonaws.com"
S3_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
EASTERN = ZoneInfo("America/New_York")


def utc_to_eastern_filter(value: str) -> str:
    """'2020-08-10T15:58:52Z' -> '2020-08-10 11:58:52' (Regulations.gov lastModifiedDate filter format)."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(EASTERN).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class CommentRecord:
    comment_id: str
    object_id: str | None
    docket_id: str | None
    comment_on_id: str | None
    posted_date: str | None
    last_modified: str | None
    title: str | None
    text: str | None
    organization: str | None
    has_attachments: bool
    withdrawn: bool
    duplicate_comments: int | None
    source: str

    @classmethod
    def from_v4(cls, data: dict[str, Any], source: str) -> CommentRecord:
        a = data.get("attributes") or {}
        rel = data.get("relationships") or {}
        attachments = (rel.get("attachments") or {}).get("data") or []
        dup = a.get("duplicateComments")
        try:
            dup_i = int(dup) if dup not in (None, "") else None
        except (TypeError, ValueError):
            dup_i = None
        return cls(
            comment_id=str(data.get("id")), object_id=a.get("objectId"), docket_id=a.get("docketId"),
            comment_on_id=a.get("commentOnDocumentId") or a.get("commentOnId"),
            posted_date=a.get("postedDate"), last_modified=a.get("modifyDate") or a.get("lastModifiedDate"),
            title=a.get("title"), text=a.get("comment"), organization=a.get("organization"),
            has_attachments=bool(attachments) or bool(a.get("attachments")),
            withdrawn=bool(a.get("withdrawn")), duplicate_comments=dup_i, source=source,
        )


class RegulationsGov:
    def __init__(self, fetcher: Fetcher, api_key: str | None):
        self.fetcher = fetcher
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise MissingCredential("REGULATIONS_GOV_API_KEY",
                                    "Free key from https://open.gsa.gov/api/regulationsgov/ (api.data.gov signup). "
                                    "Or use source='mirrulations' (no key) or ingest_bulk_csv.")
        return {"X-Api-Key": self.api_key, "Accept": "application/vnd.api+json"}

    async def _get(self, path: str, params: dict[str, Any] | None = None, cache_ttl: float = 6 * 3600) -> tuple[dict, FetchResult]:
        res = await self.fetcher.get(f"{API}{path}", params=params, headers=self._headers(), cache_ttl=cache_ttl)
        if res.status == 404:
            raise FetchError("not found on Regulations.gov", url=res.url, status=404, code="not_found")
        if res.status >= 400:
            raise FetchError(f"Regulations.gov HTTP {res.status}: {res.text()[:200]}", url=res.url, status=res.status)
        return res.json(), res

    async def docket(self, docket_id: str) -> tuple[dict, FetchResult]:
        return await self._get(f"/dockets/{docket_id}")

    async def documents(self, docket_id: str) -> tuple[list[dict], list[FetchResult]]:
        docs: list[dict] = []
        results = []
        for page in range(1, 21):
            payload, res = await self._get("/documents", {"filter[docketId]": docket_id, "page[size]": 250,
                                                          "page[number]": page})
            results.append(res)
            docs.extend(payload.get("data") or [])
            if not (payload.get("meta") or {}).get("hasNextPage"):
                break
        return docs, results

    async def comment_count(self, object_id: str) -> tuple[int | None, FetchResult]:
        payload, res = await self._get("/comments", {"filter[commentOnId]": object_id, "page[size]": 5})
        return (payload.get("meta") or {}).get("totalElements"), res

    async def comment_list_page(self, object_id: str, page: int, since_lmd_utc: str | None) -> tuple[dict, FetchResult]:
        params: dict[str, Any] = {"filter[commentOnId]": object_id, "page[size]": 250, "page[number]": page,
                                  "sort": "lastModifiedDate,documentId"}
        if since_lmd_utc:
            params["filter[lastModifiedDate][ge]"] = utc_to_eastern_filter(since_lmd_utc)
        return await self._get("/comments", params, cache_ttl=3600)

    async def comment_detail(self, comment_id: str) -> tuple[dict, FetchResult]:
        payload, res = await self._get(f"/comments/{comment_id}", {"include": "attachments"}, cache_ttl=30 * 86400)
        return payload.get("data") or {}, res


class Mirrulations:
    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher

    @staticmethod
    def prefix(docket_id: str) -> str:
        agency = docket_id.split("-")[0]
        return f"raw-data/{agency}/{docket_id}/text-{docket_id}/comments/"

    async def list_comment_keys(self, docket_id: str, *, start_after: str | None = None,
                                max_pages: int = 50) -> AsyncIterator[tuple[list[str], str | None]]:
        token = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"list-type": "2", "prefix": self.prefix(docket_id), "max-keys": "1000"}
            if token:
                params["continuation-token"] = token
            elif start_after:
                params["start-after"] = start_after
            res = await self.fetcher.get(MIRROR + "/", params=params, cache_ttl=6 * 3600)
            if res.status != 200:
                raise FetchError(f"Mirrulations listing HTTP {res.status}", url=res.url, status=res.status)
            root = ET.fromstring(res.content)
            keys = [c.find("s3:Key", S3_NS).text for c in root.findall("s3:Contents", S3_NS)]  # type: ignore[union-attr]
            nxt = root.find("s3:NextContinuationToken", S3_NS)
            yield [k for k in keys if k and k.endswith(".json")], (nxt.text if nxt is not None else None)
            if nxt is None:
                return
            token = nxt.text

    async def comment(self, key: str) -> tuple[dict, FetchResult]:
        res = await self.fetcher.get(f"{MIRROR}/{key}", cache_ttl=30 * 86400)
        if res.status != 200:
            raise FetchError(f"Mirrulations HTTP {res.status}", url=res.url, status=res.status)
        return (res.json().get("data") or {}), res


# Column names of the Regulations.gov bulk-download CSV (verified against published tooling).
CSV_COLUMNS = {
    "comment_id": ["Document ID", "document_id", "id"],
    "text": ["Comment", "comment", "text"],
    "organization": ["Organization Name", "organization"],
    "posted_date": ["Posted Date", "posted_date"],
    "docket_id": ["Docket ID", "docket_id"],
    "comment_on_id": ["Comment on Document ID", "comment_on_document_id"],
    "title": ["Title", "title"],
    "attachments": ["Attachment Files", "attachments"],
    "withdrawn": ["Is Withdrawn?", "withdrawn"],
    "duplicates": ["Duplicate Comments"],
}


def read_bulk_csv(path: Path) -> tuple[list[CommentRecord], dict[str, str | None]]:
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    mapping: dict[str, str | None] = {}
    for key, options in CSV_COLUMNS.items():
        mapping[key] = next((h for h in options if h in headers), None)
    if not mapping["comment_id"] or not mapping["text"]:
        raise ValueError(f"CSV lacks a comment ID or comment text column; found columns: {headers[:15]}")
    records = []
    seen: dict[str, int] = {}
    for row in reader:
        def g(k: str) -> str | None:
            col = mapping.get(k)
            val = row.get(col) if col else None
            return val.strip() if isinstance(val, str) and val.strip() else None
        cid = g("comment_id")
        if not cid:
            continue
        # Regulations.gov occasionally reuses Document IDs; keep every row distinct.
        if cid in seen:
            seen[cid] += 1
            cid = f"{cid}#{seen[cid]}"
        else:
            seen[cid] = 0
        dup = g("duplicates")
        records.append(CommentRecord(
            comment_id=cid, object_id=None, docket_id=g("docket_id"), comment_on_id=g("comment_on_id"),
            posted_date=g("posted_date"), last_modified=None, title=g("title"), text=g("text"),
            organization=g("organization"), has_attachments=bool(g("attachments")),
            withdrawn=(g("withdrawn") or "").lower() == "true",
            duplicate_comments=int(dup) if dup and dup.isdigit() else None, source="bulk_csv",
        ))
    return records, mapping
