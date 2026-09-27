"""Internet Archive Wayback CDX client (shared by SiteGraph and ClaimTrail).

Uses the documented CDX server API: ``output=json`` returns a header row followed
by data rows; ``fl`` selects fields; ``filter`` / ``collapse`` / ``matchType``
narrow results.
"""

from __future__ import annotations

from dataclasses import dataclass

from .http import Fetcher, FetchError, FetchResult

CDX_URL = "https://web.archive.org/cdx/search/cdx"


@dataclass
class Capture:
    timestamp: str  # YYYYMMDDhhmmss
    original: str
    statuscode: str | None
    digest: str | None
    mimetype: str | None

    @property
    def iso(self) -> str:
        t = self.timestamp.ljust(14, "0")
        return f"{t[0:4]}-{t[4:6]}-{t[6:8]}T{t[8:10]}:{t[10:12]}:{t[12:14]}Z"

    @property
    def replay_url(self) -> str:
        return f"https://web.archive.org/web/{self.timestamp}/{self.original}"


def parse_cdx_json(payload: list) -> list[Capture]:
    if not payload:
        return []
    header = payload[0]
    idx = {name: i for i, name in enumerate(header)}
    out = []
    for row in payload[1:]:
        def g(name: str) -> str | None:
            i = idx.get(name)
            return row[i] if i is not None and i < len(row) and row[i] != "-" else None
        ts = g("timestamp")
        orig = g("original")
        if ts and orig:
            out.append(Capture(ts, orig, g("statuscode"), g("digest"), g("mimetype")))
    return out


async def cdx_query(fetcher: Fetcher, url: str, *, limit: int = 50, from_ts: str | None = None,
                    to_ts: str | None = None, match_type: str | None = None, only_ok: bool = True,
                    collapse: str | None = None, reverse: bool = False) -> tuple[list[Capture], FetchResult]:
    params: dict[str, object] = {
        "url": url, "output": "json", "fl": "timestamp,original,statuscode,digest,mimetype",
        "limit": -abs(limit) if reverse else limit,
    }
    if from_ts:
        params["from"] = from_ts
    if to_ts:
        params["to"] = to_ts
    if match_type:
        params["matchType"] = match_type
    if only_ok:
        params["filter"] = "statuscode:200"
    if collapse:
        params["collapse"] = collapse
    res = await fetcher.get(CDX_URL, params=params, max_bytes=8 * 1024 * 1024)
    if res.status != 200:
        raise FetchError(f"Wayback CDX HTTP {res.status}", url=res.url, status=res.status)
    text = res.text().strip()
    if not text:
        return [], res
    return parse_cdx_json(res.json()), res
