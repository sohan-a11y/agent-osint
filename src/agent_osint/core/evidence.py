"""Evidence records: every claim a tool makes points at one of these.

An evidence record stores the URL, fetch time, HTTP status, SHA-256 of the raw
bytes and (optionally, capped) the bytes themselves, so an agent or a human can
re-check exactly what the tool saw.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .http import FetchResult, utcnow_iso

MAX_STORED_BODY = 2 * 1024 * 1024


@dataclass
class Evidence:
    id: str
    url: str
    fetched_at: str
    status: int | None
    sha256: str | None
    content_type: str | None
    note: str | None = None
    excerpt: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


class EvidenceStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, url TEXT, fetched_at TEXT, status INTEGER,"
            " sha256 TEXT, content_type TEXT, note TEXT, excerpt TEXT, body BLOB)"
        )
        self._conn.commit()

    def record(self, result: FetchResult, *, note: str | None = None, excerpt: str | None = None,
               keep_body: bool = True) -> Evidence:
        digest = result.sha256
        eid = hashlib.sha256(f"{result.url}|{result.fetched_at}|{digest}".encode()).hexdigest()[:20]
        ev = Evidence(id=eid, url=result.url, fetched_at=result.fetched_at, status=result.status,
                      sha256=digest, content_type=result.content_type or None, note=note,
                      excerpt=_clip(excerpt))
        body = result.content[:MAX_STORED_BODY] if keep_body else None
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO evidence VALUES (?,?,?,?,?,?,?,?,?)",
                (ev.id, ev.url, ev.fetched_at, ev.status, ev.sha256, ev.content_type, ev.note, ev.excerpt, body),
            )
            self._conn.commit()
        return ev

    def record_derived(self, url: str, *, note: str, excerpt: str | None = None,
                       payload: bytes | None = None) -> Evidence:
        """Evidence for data that did not come from a single HTTP fetch (e.g. a local file)."""
        digest = hashlib.sha256(payload).hexdigest() if payload is not None else None
        fetched = utcnow_iso()
        eid = hashlib.sha256(f"{url}|{fetched}|{digest}|{note}".encode()).hexdigest()[:20]
        ev = Evidence(id=eid, url=url, fetched_at=fetched, status=None, sha256=digest, content_type=None,
                      note=note, excerpt=_clip(excerpt))
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO evidence VALUES (?,?,?,?,?,?,?,?,?)",
                (ev.id, ev.url, ev.fetched_at, None, ev.sha256, None, ev.note, ev.excerpt,
                 payload[:MAX_STORED_BODY] if payload else None),
            )
            self._conn.commit()
        return ev

    def get(self, eid: str, *, include_body: bool = True, max_chars: int = 20000) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id,url,fetched_at,status,sha256,content_type,note,excerpt,body FROM evidence WHERE id=?",
                (eid,),
            ).fetchone()
        if not row:
            return None
        out: dict[str, Any] = {
            "id": row[0], "url": row[1], "fetched_at": row[2], "status": row[3], "sha256": row[4],
            "content_type": row[5], "note": row[6], "excerpt": row[7],
        }
        if include_body and row[8] is not None:
            text = row[8].decode("utf-8", errors="replace")
            out["body"] = text[:max_chars]
            out["body_truncated"] = len(text) > max_chars
        return {k: v for k, v in out.items() if v is not None}


def _clip(text: str | None, limit: int = 500) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
