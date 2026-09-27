"""sellers.json retrieval and lookup (IAB Tech Lab sellers.json 1.0).

sellers.json files can be very large (Google's is hundreds of MB), so each file
is streamed to disk, parsed incrementally with ijson and indexed into SQLite.
Lookups afterwards are instant. Indexes are refreshed after ``ttl_days``.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import ijson

from ..core.http import Fetcher, FetchError, utcnow_iso

# Ad systems whose sellers.json is not at https://<domain>/sellers.json.
SELLERS_JSON_LOCATIONS: dict[str, str] = {
    "google.com": "https://realtimebidding.google.com/sellers.json",
}


@dataclass
class SellerRecord:
    ad_system: str
    seller_id: str
    seller_type: str | None
    name: str | None
    domain: str | None
    is_confidential: bool
    source_url: str
    indexed_at: str


class SellersIndex:
    def __init__(self, directory: Path, fetcher: Fetcher, *, ttl_days: float = 7.0,
                 max_bytes: int = 800 * 1024 * 1024):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.fetcher = fetcher
        self.ttl = ttl_days * 86400
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(directory / "sellers.sqlite"), check_same_thread=False)
        self._conn.executescript(
            "CREATE TABLE IF NOT EXISTS files (ad_system TEXT PRIMARY KEY, url TEXT, indexed_at TEXT, stored REAL,"
            " n_sellers INTEGER, contact_email TEXT, error TEXT, sha256 TEXT);"
            "CREATE TABLE IF NOT EXISTS sellers (ad_system TEXT, seller_id TEXT, seller_type TEXT, name TEXT,"
            " domain TEXT, is_confidential INTEGER, PRIMARY KEY (ad_system, seller_id));"
            "CREATE INDEX IF NOT EXISTS sellers_domain ON sellers(domain);"
        )
        self._conn.commit()

    def url_for(self, ad_system: str) -> str:
        return SELLERS_JSON_LOCATIONS.get(ad_system, f"https://{ad_system}/sellers.json")

    def _fresh(self, ad_system: str) -> tuple[bool, str | None]:
        row = self._conn.execute("SELECT stored, error FROM files WHERE ad_system=?", (ad_system,)).fetchone()
        if not row:
            return False, None
        if time.time() - row[0] > self.ttl:
            return False, None
        return True, row[1]

    async def ensure(self, ad_system: str) -> str | None:
        """Make sure the ad system's sellers.json is indexed. Returns an error string or None."""
        fresh, err = self._fresh(ad_system)
        if fresh:
            return err
        url = self.url_for(ad_system)
        dest = self.dir / "files" / f"{ad_system}.json"
        error = None
        count = 0
        contact = None
        sha = None
        try:
            await self.fetcher.download(url, dest, max_bytes=self.max_bytes)
            count, contact, sha = self._index_file(ad_system, dest)
        except FetchError as exc:
            error = str(exc)
        except (ijson.JSONError, ValueError, UnicodeDecodeError) as exc:
            error = f"invalid sellers.json: {exc}"
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?)",
                               (ad_system, url, utcnow_iso(), time.time(), count, contact, error, sha))
            self._conn.commit()
        if dest.exists():
            dest.unlink()  # the SQLite index is what we keep
        return error

    def _index_file(self, ad_system: str, path: Path) -> tuple[int, str | None, str]:
        import hashlib

        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        contact = None
        rows: list[tuple] = []
        total = 0
        with path.open("rb") as fh:
            for prefix, event, value in ijson.parse(fh, use_float=True):
                if prefix == "contact_email" and event == "string":
                    contact = value
                    break
                if prefix == "sellers" and event == "start_array":
                    break
        with self._lock:
            self._conn.execute("DELETE FROM sellers WHERE ad_system=?", (ad_system,))
            self._conn.commit()
        with path.open("rb") as fh:
            for seller in ijson.items(fh, "sellers.item", use_float=True):
                if not isinstance(seller, dict) or seller.get("seller_id") in (None, ""):
                    continue
                conf = seller.get("is_confidential", 0)
                rows.append((
                    ad_system, str(seller.get("seller_id")).strip(),
                    str(seller.get("seller_type") or "").upper() or None,
                    (str(seller["name"]).strip() if seller.get("name") else None),
                    (str(seller["domain"]).strip().lower() if seller.get("domain") else None),
                    1 if str(conf) in ("1", "True", "true") else 0,
                ))
                if len(rows) >= 50000:  # stream to SQLite; Google's file has millions of sellers
                    total += self._flush(rows)
        total += self._flush(rows)
        return total, contact, digest.hexdigest()

    def _flush(self, rows: list[tuple]) -> int:
        n = len(rows)
        if n:
            with self._lock:
                self._conn.executemany("INSERT OR REPLACE INTO sellers VALUES (?,?,?,?,?,?)", rows)
                self._conn.commit()
            rows.clear()
        return n

    def lookup(self, ad_system: str, seller_id: str) -> SellerRecord | None:
        row = self._conn.execute(
            "SELECT s.seller_type, s.name, s.domain, s.is_confidential, f.url, f.indexed_at FROM sellers s "
            "JOIN files f ON f.ad_system = s.ad_system WHERE s.ad_system=? AND s.seller_id=?",
            (ad_system, seller_id.strip()),
        ).fetchone()
        if not row:
            return None
        return SellerRecord(ad_system, seller_id, row[0], row[1], row[2], bool(row[3]), row[4], row[5])

    def by_domain(self, domain: str, limit: int = 50) -> list[SellerRecord]:
        rows = self._conn.execute(
            "SELECT s.ad_system, s.seller_id, s.seller_type, s.name, s.domain, s.is_confidential, f.url, f.indexed_at"
            " FROM sellers s JOIN files f ON f.ad_system = s.ad_system WHERE s.domain=? LIMIT ?",
            (domain.lower(), limit),
        ).fetchall()
        return [SellerRecord(*r[:5], bool(r[5]), r[6], r[7]) for r in rows]

    def file_info(self, ad_system: str) -> dict | None:
        row = self._conn.execute("SELECT url, indexed_at, n_sellers, error, sha256 FROM files WHERE ad_system=?",
                                 (ad_system,)).fetchone()
        if not row:
            return None
        return {"url": row[0], "indexed_at": row[1], "n_sellers": row[2], "error": row[3], "sha256": row[4]}
