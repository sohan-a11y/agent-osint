"""User-supplied datasets: Retraction Watch CSV, Problematic Paper Screener export, hijacked-journal list.

These sources have no public API (or their reuse terms must be accepted by the user), so the
operator downloads them and loads them with ``load_dataset``. Columns are detected by name so that
minor format changes don't break loading.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from ..core.domains import registrable_domain
from ..core.http import utcnow_iso
from .sources import extract_dois

ISSN_RE = re.compile(r"\b(\d{4}-?\d{3}[\dXx])\b")
URL_HOST_RE = re.compile(r"(?:https?://)?(?:www\.)?([a-z0-9.-]+\.[a-z]{2,24})", re.IGNORECASE)


def _norm_issn(v: str) -> str:
    v = v.upper().replace("-", "")
    return f"{v[:4]}-{v[4:]}"


def _find_col(headers: list[str], *needles: str, exclude: tuple[str, ...] = ()) -> str | None:
    low = {h: h.lower().replace(" ", "").replace("_", "") for h in headers}
    for needle in needles:
        n = needle.lower().replace(" ", "").replace("_", "")
        for h, lowered in low.items():
            if n in lowered and not any(x in lowered for x in exclude):
                return h
    return None


class LocalData:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.executescript(
            "CREATE TABLE IF NOT EXISTS datasets (kind TEXT PRIMARY KEY, path TEXT, loaded_at TEXT, rows INTEGER, columns TEXT);"
            "CREATE TABLE IF NOT EXISTS rw (doi TEXT, notice_doi TEXT, nature TEXT, date TEXT, reason TEXT, journal TEXT, record_id TEXT);"
            "CREATE INDEX IF NOT EXISTS rw_doi ON rw(doi);"
            "CREATE INDEX IF NOT EXISTS rw_journal ON rw(journal);"
            "CREATE TABLE IF NOT EXISTS pps (doi TEXT, flags TEXT);"
            "CREATE INDEX IF NOT EXISTS pps_doi ON pps(doi);"
            "CREATE TABLE IF NOT EXISTS hijacked (title TEXT, issn TEXT, authentic TEXT, hijacked_domains TEXT);"
        )
        self.conn.commit()

    def status(self) -> dict[str, Any]:
        rows = self.conn.execute("SELECT kind, path, loaded_at, rows FROM datasets").fetchall()
        return {r[0]: {"path": r[1], "loaded_at": r[2], "rows": r[3]} for r in rows}

    def _read(self, path: Path) -> tuple[list[dict[str, str]], list[str]]:
        text = path.read_bytes().decode("utf-8-sig", errors="replace")
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        rows = list(reader)
        return rows, list(reader.fieldnames or [])

    def load(self, kind: str, path: str) -> dict[str, Any]:
        p = Path(path).expanduser()
        if not p.is_file():
            raise ValueError(f"file not found: {path}")
        rows, headers = self._read(p)
        if kind == "retraction_watch":
            n, cols = self._load_rw(rows, headers)
        elif kind == "problematic_paper_screener":
            n, cols = self._load_pps(rows, headers)
        elif kind == "hijacked_journals":
            n, cols = self._load_hijacked(rows, headers)
        else:
            raise ValueError("kind must be retraction_watch, problematic_paper_screener or hijacked_journals")
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO datasets VALUES (?,?,?,?,?)",
                              (kind, str(p.resolve()), utcnow_iso(), n, json.dumps(cols)))
            self.conn.commit()
        return {"kind": kind, "rows_loaded": n, "columns_used": cols}

    def _load_rw(self, rows: list[dict], headers: list[str]) -> tuple[int, dict]:
        cols = {
            "doi": _find_col(headers, "OriginalPaperDOI"),
            "notice": _find_col(headers, "RetractionDOI"),
            "nature": _find_col(headers, "RetractionNature", "Nature"),
            "date": _find_col(headers, "RetractionDate"),
            "reason": _find_col(headers, "Reason"),
            "journal": _find_col(headers, "Journal"),
            "id": _find_col(headers, "RecordID", "Record ID"),
        }
        if not cols["doi"]:
            raise ValueError(f"Retraction Watch CSV needs an OriginalPaperDOI column; found {headers[:20]}")
        data = []
        for r in rows:
            doi = (r.get(cols["doi"]) or "").strip().lower()
            if not doi or doi in {"unavailable", "none"}:
                continue
            g = lambda k: (r.get(cols[k]) or "").strip() if cols[k] else None  # noqa: E731
            data.append((doi, (g("notice") or "").lower() or None, g("nature"), g("date"), g("reason"),
                         g("journal"), g("id")))
        with self._lock:
            self.conn.execute("DELETE FROM rw")
            self.conn.executemany("INSERT INTO rw VALUES (?,?,?,?,?,?,?)", data)
            self.conn.commit()
        return len(data), cols

    def _load_pps(self, rows: list[dict], headers: list[str]) -> tuple[int, dict]:
        doi_col = _find_col(headers, "doi")
        if not doi_col:
            raise ValueError(f"PPS export needs a DOI column; found {headers[:20]}")
        flag_cols = [h for h in headers if h != doi_col]
        data = []
        for r in rows:
            dois = extract_dois(r.get(doi_col) or "")
            if not dois:
                continue
            flags = {h: r[h] for h in flag_cols if r.get(h) not in (None, "", "0", "false", "False")}
            data.append((dois[0], json.dumps(flags)[:4000]))
        with self._lock:
            self.conn.execute("DELETE FROM pps")
            self.conn.executemany("INSERT INTO pps VALUES (?,?)", data)
            self.conn.commit()
        return len(data), {"doi": doi_col, "flag_columns": flag_cols[:20]}

    def _load_hijacked(self, rows: list[dict], headers: list[str]) -> tuple[int, dict]:
        cols = {
            "title": _find_col(headers, "journal title", "title", "journal", exclude=("url", "site", "hijack")),
            "issn": _find_col(headers, "issn"),
            "authentic": _find_col(headers, "authentic", "legitimate", "original", "real"),
            "hijacked": _find_col(headers, "hijacked", "fake", "clone", exclude=("title",)),
        }
        if not cols["hijacked"]:
            raise ValueError(f"hijacked-journals file needs a column with hijacked URLs; found {headers[:20]}")
        data = []
        for r in rows:
            hij_raw = r.get(cols["hijacked"]) or ""
            hosts = sorted({registrable_domain(m.group(1).lower()) or m.group(1).lower()
                            for m in URL_HOST_RE.finditer(hij_raw)})
            if not hosts:
                continue
            issns = sorted({_norm_issn(m.group(1)) for m in ISSN_RE.finditer(r.get(cols["issn"]) or "")}) if cols["issn"] else []
            auth = (r.get(cols["authentic"]) or "").strip() if cols["authentic"] else ""
            data.append(((r.get(cols["title"]) or "").strip() if cols["title"] else "", ",".join(issns), auth,
                         ",".join(hosts)))
        with self._lock:
            self.conn.execute("DELETE FROM hijacked")
            self.conn.executemany("INSERT INTO hijacked VALUES (?,?,?,?)", data)
            self.conn.commit()
        return len(data), cols

    # lookups --------------------------------------------------------------
    def rw_for(self, doi: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT notice_doi, nature, date, reason, record_id FROM rw WHERE doi=?", (doi,)).fetchall()
        return [dict(zip(["notice_doi", "nature", "date", "reason", "record_id"], r)) for r in rows]

    def rw_journal_count(self, journal: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM rw WHERE lower(journal)=lower(?)", (journal,)).fetchone()[0]

    def pps_for(self, doi: str) -> list[dict[str, Any]]:
        return [json.loads(r[0]) for r in self.conn.execute("SELECT flags FROM pps WHERE doi=?", (doi,)).fetchall()]

    def hijacked_match(self, *, issn: str | None = None, domain: str | None = None,
                       title: str | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT title, issn, authentic, hijacked_domains FROM hijacked").fetchall()
        out = []
        for t, issns, auth, hosts in rows:
            reasons = []
            if issn and issn in (issns or "").split(","):
                reasons.append("issn")
            if domain and domain in (hosts or "").split(","):
                reasons.append("hijacked_domain")
            if title and t and title.strip().lower() == t.strip().lower():
                reasons.append("title")
            if reasons:
                out.append({"journal_title": t, "issns": [i for i in (issns or "").split(",") if i],
                            "authentic_site": auth or None, "hijacked_domains": hosts.split(","),
                            "matched_on": reasons})
        return out

    def has(self, kind: str) -> bool:
        return self.conn.execute("SELECT 1 FROM datasets WHERE kind=?", (kind,)).fetchone() is not None
