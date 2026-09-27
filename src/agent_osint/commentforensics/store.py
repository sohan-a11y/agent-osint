"""Local SQLite store for dockets, comments and resumable ingestion state.

Submitter first/last names, addresses, emails and phone numbers are never stored.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .sources import CommentRecord


class CommentStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.executescript(
            "PRAGMA journal_mode=WAL;"
            "CREATE TABLE IF NOT EXISTS comments (docket_id TEXT, comment_id TEXT, object_id TEXT,"
            " comment_on_id TEXT, posted_date TEXT, last_modified TEXT, title TEXT, text TEXT, organization TEXT,"
            " has_attachments INTEGER, withdrawn INTEGER, duplicate_comments INTEGER, source TEXT,"
            " detail_fetched INTEGER DEFAULT 0, evidence_id TEXT, PRIMARY KEY (docket_id, comment_id));"
            "CREATE INDEX IF NOT EXISTS comments_detail ON comments(docket_id, detail_fetched);"
            "CREATE TABLE IF NOT EXISTS documents (docket_id TEXT, document_id TEXT, object_id TEXT, document_type TEXT,"
            " title TEXT, comment_start TEXT, comment_end TEXT, total_comments INTEGER, PRIMARY KEY (docket_id, document_id));"
            "CREATE TABLE IF NOT EXISTS ingest_state (docket_id TEXT, key TEXT, value TEXT, PRIMARY KEY (docket_id, key));"
            "CREATE TABLE IF NOT EXISTS dockets (docket_id TEXT PRIMARY KEY, data TEXT, fetched_at REAL);"
            "CREATE TABLE IF NOT EXISTS cluster_cache (docket_id TEXT, params TEXT, n_comments INTEGER, created REAL,"
            " result TEXT, PRIMARY KEY (docket_id, params));"
            "CREATE TABLE IF NOT EXISTS stats (docket_id TEXT PRIMARY KEY, mismatched INTEGER DEFAULT 0);"
        )
        self.conn.commit()

    # state -----------------------------------------------------------------
    def get_state(self, docket_id: str, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM ingest_state WHERE docket_id=? AND key=?", (docket_id, key)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, docket_id: str, key: str, value: Any) -> None:
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO ingest_state VALUES (?,?,?)", (docket_id, key, json.dumps(value)))
            self.conn.commit()

    def add_mismatch(self, docket_id: str, n: int = 1) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO stats VALUES (?,?) ON CONFLICT(docket_id) DO UPDATE SET"
                              " mismatched = mismatched + excluded.mismatched", (docket_id, n))
            self.conn.commit()

    def mismatches(self, docket_id: str) -> int:
        row = self.conn.execute("SELECT mismatched FROM stats WHERE docket_id=?", (docket_id,)).fetchone()
        return row[0] if row else 0

    # dockets/documents -----------------------------------------------------
    def save_docket(self, docket_id: str, data: dict) -> None:
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO dockets VALUES (?,?,?)", (docket_id, json.dumps(data), time.time()))
            self.conn.commit()

    def docket(self, docket_id: str) -> dict | None:
        row = self.conn.execute("SELECT data FROM dockets WHERE docket_id=?", (docket_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_documents(self, docket_id: str, docs: list[dict]) -> None:
        with self._lock:
            for d in docs:
                a = d.get("attributes") or {}
                self.conn.execute(
                    "INSERT INTO documents VALUES (?,?,?,?,?,?,?,NULL) ON CONFLICT(docket_id, document_id) DO UPDATE SET"
                    " object_id=excluded.object_id, document_type=excluded.document_type, title=excluded.title,"
                    " comment_start=excluded.comment_start, comment_end=excluded.comment_end",
                    (docket_id, d.get("id"), a.get("objectId"), a.get("documentType"), a.get("title"),
                     a.get("commentStartDate"), a.get("commentEndDate")),
                )
            self.conn.commit()

    def set_document_total(self, docket_id: str, object_id: str, total: int | None) -> None:
        with self._lock:
            self.conn.execute("UPDATE documents SET total_comments=? WHERE docket_id=? AND object_id=?",
                              (total, docket_id, object_id))
            self.conn.commit()

    def documents(self, docket_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT document_id, object_id, document_type, title, comment_start, comment_end, total_comments"
            " FROM documents WHERE docket_id=? ORDER BY document_id", (docket_id,)).fetchall()
        keys = ["document_id", "object_id", "document_type", "title", "comment_start", "comment_end", "total_comments"]
        return [dict(zip(keys, r)) for r in rows]

    # comments --------------------------------------------------------------
    def upsert_listed(self, docket_id: str, items: Iterable[dict], comment_on_id: str) -> int:
        n = 0
        with self._lock:
            for item in items:
                a = item.get("attributes") or {}
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO comments (docket_id, comment_id, object_id, comment_on_id, posted_date,"
                    " last_modified, title, withdrawn, source, detail_fetched) VALUES (?,?,?,?,?,?,?,?,?,0)",
                    (docket_id, item.get("id"), a.get("objectId"), comment_on_id, a.get("postedDate"),
                     a.get("lastModifiedDate"), a.get("title"), int(bool(a.get("withdrawn"))), "api_list"),
                )
                n += cur.rowcount
            self.conn.commit()
        return n

    def upsert_full(self, docket_id: str, rec: CommentRecord, evidence_id: str | None = None) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO comments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?) ON CONFLICT(docket_id, comment_id) DO UPDATE SET"
                " object_id=COALESCE(excluded.object_id, comments.object_id), comment_on_id=COALESCE(excluded.comment_on_id,"
                " comments.comment_on_id), posted_date=COALESCE(excluded.posted_date, comments.posted_date),"
                " last_modified=COALESCE(excluded.last_modified, comments.last_modified), title=excluded.title,"
                " text=excluded.text, organization=excluded.organization, has_attachments=excluded.has_attachments,"
                " withdrawn=excluded.withdrawn, duplicate_comments=excluded.duplicate_comments, source=excluded.source,"
                " detail_fetched=1, evidence_id=COALESCE(excluded.evidence_id, comments.evidence_id)",
                (docket_id, rec.comment_id, rec.object_id, rec.comment_on_id, rec.posted_date, rec.last_modified,
                 rec.title, rec.text, rec.organization, int(rec.has_attachments), int(rec.withdrawn),
                 rec.duplicate_comments, rec.source, evidence_id),
            )
            self.conn.commit()

    def pending_detail(self, docket_id: str, limit: int) -> list[str]:
        rows = self.conn.execute("SELECT comment_id FROM comments WHERE docket_id=? AND detail_fetched=0 LIMIT ?",
                                 (docket_id, limit)).fetchall()
        return [r[0] for r in rows]

    def counts(self, docket_id: str) -> dict[str, int]:
        row = self.conn.execute(
            "SELECT COUNT(*), SUM(detail_fetched), SUM(CASE WHEN detail_fetched=1 AND (text IS NULL OR text='') THEN 1 ELSE 0 END)"
            " FROM comments WHERE docket_id=?", (docket_id,)).fetchone()
        return {"known_comments": row[0] or 0, "with_text": (row[1] or 0) - (row[2] or 0), "fetched": row[1] or 0}

    def texts(self, docket_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT comment_id, text, organization, posted_date, has_attachments, withdrawn, evidence_id,"
            " duplicate_comments FROM comments WHERE docket_id=? AND detail_fetched=1 ORDER BY comment_id",
            (docket_id,)).fetchall()
        keys = ["comment_id", "text", "organization", "posted_date", "has_attachments", "withdrawn", "evidence_id",
                "duplicate_comments"]
        return [dict(zip(keys, r)) for r in rows]

    def fingerprint(self, docket_id: str) -> str:
        row = self.conn.execute("SELECT COUNT(*), MAX(comment_id), SUM(LENGTH(text)) FROM comments WHERE docket_id=?"
                                " AND detail_fetched=1", (docket_id,)).fetchone()
        return hashlib.sha1(json.dumps(row).encode()).hexdigest()[:12]

    def cached_clusters(self, docket_id: str, params: str, fp: str) -> dict | None:
        row = self.conn.execute("SELECT result FROM cluster_cache WHERE docket_id=? AND params=?",
                                (docket_id, f"{params}|{fp}")).fetchone()
        return json.loads(row[0]) if row else None

    def save_clusters(self, docket_id: str, params: str, fp: str, result: dict) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM cluster_cache WHERE docket_id=? AND params LIKE ?", (docket_id, f"{params}|%"))
            self.conn.execute("INSERT OR REPLACE INTO cluster_cache VALUES (?,?,?,?,?)",
                              (docket_id, f"{params}|{fp}", 0, time.time(), json.dumps(result)))
            self.conn.commit()
