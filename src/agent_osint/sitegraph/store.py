"""SiteGraph's persistent identifier index (the thing live APIs don't give you)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..core.http import utcnow_iso


class SignalStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.executescript(
            "PRAGMA journal_mode=WAL;"
            "CREATE TABLE IF NOT EXISTS signals (domain TEXT, kind TEXT, value TEXT, evidence_id TEXT, source TEXT,"
            " first_seen TEXT, last_seen TEXT, PRIMARY KEY (domain, kind, value));"
            "CREATE INDEX IF NOT EXISTS signals_kv ON signals(kind, value);"
            "CREATE TABLE IF NOT EXISTS fingerprints (domain TEXT PRIMARY KEY, root TEXT, fetched_at TEXT,"
            " stored REAL, data TEXT);"
        )
        self._conn.commit()

    def add_signals(self, domain: str, signals: Iterable[tuple[str, str, str | None]], source: str) -> int:
        now = utcnow_iso()
        n = 0
        with self._lock:
            for kind, value, evidence_id in signals:
                if not value:
                    continue
                self._conn.execute(
                    "INSERT INTO signals VALUES (?,?,?,?,?,?,?) ON CONFLICT(domain, kind, value) DO UPDATE SET"
                    " last_seen=excluded.last_seen, evidence_id=COALESCE(excluded.evidence_id, signals.evidence_id),"
                    " source=excluded.source",
                    (domain, kind, value, evidence_id, source, now, now),
                )
                n += 1
            self._conn.commit()
        return n

    def replace_live_signals(self, domain: str, signals: list[tuple[str, str, str | None]], source: str) -> None:
        """Replace signals from a live fingerprint, keeping history from other sources (e.g. Common Crawl)."""
        with self._lock:
            self._conn.execute("DELETE FROM signals WHERE domain=? AND source=?", (domain, source))
            self._conn.commit()
        self.add_signals(domain, signals, source)

    def signals_for(self, domain: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT kind, value, evidence_id, source, first_seen, last_seen FROM signals WHERE domain=?", (domain,)
        ).fetchall()
        return [dict(zip(["kind", "value", "evidence_id", "source", "first_seen", "last_seen"], r)) for r in rows]

    def domains_with(self, kind: str, value: str, limit: int = 5000) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT domain, evidence_id, source, first_seen, last_seen FROM signals WHERE kind=? AND value=? LIMIT ?",
            (kind, value, limit),
        ).fetchall()
        return [dict(zip(["domain", "evidence_id", "source", "first_seen", "last_seen"], r)) for r in rows]

    def value_count(self, kind: str, value: str) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM signals WHERE kind=? AND value=?", (kind, value)).fetchone()[0]

    def find_value(self, value: str, limit: int = 2000) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT domain, kind, value, evidence_id FROM signals WHERE value=? LIMIT ?", (value, limit)
        ).fetchall()
        return [dict(zip(["domain", "kind", "value", "evidence_id"], r)) for r in rows]

    def save_fingerprint(self, domain: str, root: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO fingerprints VALUES (?,?,?,?,?)",
                               (domain, root, data.get("fetched_at"), time.time(), json.dumps(data)))
            self._conn.commit()

    def load_fingerprint(self, domain: str, max_age: float) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT stored, data FROM fingerprints WHERE domain=?", (domain,)).fetchone()
        if not row or time.time() - row[0] > max_age:
            return None
        return json.loads(row[1])

    def stats(self) -> dict[str, Any]:
        n_domains = self._conn.execute("SELECT COUNT(DISTINCT domain) FROM signals").fetchone()[0]
        n_fp = self._conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
        by_kind = dict(self._conn.execute("SELECT kind, COUNT(*) FROM signals GROUP BY kind").fetchall())
        by_source = dict(self._conn.execute("SELECT source, COUNT(DISTINCT domain) FROM signals GROUP BY source").fetchall())
        return {"domains_indexed": n_domains, "live_fingerprints": n_fp, "signals_by_kind": by_kind,
                "domains_by_source": by_source}
