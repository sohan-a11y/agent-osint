"""EU DSA Transparency Database: Research API client and local dump ingestion.

Research API (https://transparency.dsa.ec.europa.eu/page/research-api), verified from the page:
* base ``/api/v1/research``; header ``Authorization: Bearer <token>`` (token via EU Login + DSA helpdesk)
* ``POST /count`` and ``POST /search`` take OpenSearch DSL ``{"query": {...}}``;
  ``POST /query`` takes a DQL string; ``GET /aggregates/{date}[/{f1__f2}]`` returns
  ``{"aggregates": [{..., "total": n}], "total": n, ...}``; ``GET /platforms``; ``GET /labels``
* limits: 1,000 rows, 5 MB, 30 s per request; last 6 months only; 413/504 on overrun
* aggregate fields: automated_decision, automated_detection, category, content_type_single, decision_account,
  decision_ground, decision_monetary, decision_provision, decision_visibility_single, platform_id,
  received_date, source_type

Daily dumps (https://transparency.dsa.ec.europa.eu/data-download) are zipped CSVs (global or per platform,
"full" or "light"). Their inner layout is not documented on the download page, so ingestion walks nested
zips and reads every CSV whose header contains the statement-of-reasons columns it needs.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import duckdb

from ..core.http import Fetcher, FetchError, FetchResult
from ..core.results import MissingCredential

API = "https://transparency.dsa.ec.europa.eu/api/v1/research"
AGG_FIELDS = {"automated_decision", "automated_detection", "category", "content_type_single", "decision_account",
              "decision_ground", "decision_monetary", "decision_provision", "decision_visibility_single",
              "platform_id", "received_date", "source_type"}

# Columns kept locally (statement-of-reasons attribute names from the official API documentation).
KEEP_COLUMNS = [
    "uuid", "platform_name", "platform_uid", "created_at", "application_date", "content_date", "decision_ground",
    "category", "category_specification", "decision_visibility", "decision_monetary", "decision_provision",
    "decision_account", "account_type", "content_type", "automated_detection", "automated_decision", "source_type",
    "territorial_scope", "content_language", "incompatible_content_illegal",
]
ARRAY_COLUMNS = {"decision_visibility", "content_type", "territorial_scope", "category_specification"}


class ResearchAPI:
    def __init__(self, fetcher: Fetcher, token: str | None):
        self.fetcher = fetcher
        self.token = token

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise MissingCredential("DSA_TDB_TOKEN", "Create an EU Login account and request a Research API token "
                                    "from CNECT-DSA-HELPDESK@ec.europa.eu; or ingest daily dumps locally instead.")
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    async def _check(self, res: FetchResult) -> Any:
        if res.status == 413:
            raise FetchError("response exceeds the API's 5 MB limit; narrow the query", url=res.url, status=413)
        if res.status == 504:
            raise FetchError("query exceeded the API's 30 s limit; narrow the date range", url=res.url, status=504)
        if res.status in (401, 403):
            raise FetchError(f"DSA Research API rejected the token (HTTP {res.status})", url=res.url, status=res.status)
        if res.status != 200:
            raise FetchError(f"DSA Research API HTTP {res.status}", url=res.url, status=res.status)
        return res.json()

    async def count(self, query: dict) -> tuple[int, FetchResult]:
        res = await self.fetcher.post_json(f"{API}/count", {"query": query}, headers=self._headers(), cache_ttl=3600)
        data = await self._check(res)
        return int(((data.get("data") or {}).get("count")) or 0), res

    async def search(self, query: dict, size: int, source_fields: list[str]) -> tuple[dict, FetchResult]:
        body = {"query": query, "size": min(size, 1000), "_source": source_fields}
        res = await self.fetcher.post_json(f"{API}/search", body, headers=self._headers(), cache_ttl=3600)
        return await self._check(res), res

    async def aggregates(self, date: str, fields: list[str]) -> tuple[dict, FetchResult]:
        bad = [f for f in fields if f not in AGG_FIELDS]
        if bad:
            raise ValueError(f"not aggregatable via the API: {bad}; allowed: {sorted(AGG_FIELDS)}")
        path = f"{API}/aggregates/{date}" + (f"/{'__'.join(fields)}" if fields else "")
        res = await self.fetcher.get(path, headers=self._headers(), cache_ttl=7 * 86400)
        return await self._check(res), res

    async def platforms(self) -> tuple[Any, FetchResult]:
        res = await self.fetcher.get(f"{API}/platforms", headers=self._headers(), cache_ttl=86400)
        return await self._check(res), res


def build_query(*, platform_id: int | None = None, date_from: str | None = None, date_to: str | None = None,
                terms: dict[str, Any] | None = None) -> dict:
    must: list[dict] = []
    if platform_id is not None:
        must.append({"term": {"platform_id": platform_id}})
    for k, v in (terms or {}).items():
        if v is not None:
            must.append({"term": {k: v}})
    q: dict[str, Any] = {"bool": {"must": must}}
    if date_from or date_to:
        rng = {}
        if date_from:
            rng["gte"] = date_from
        if date_to:
            rng["lte"] = date_to
        q["bool"]["filter"] = [{"range": {"received_date": rng}}]
    return q


# ---------------------------------------------------------------------------- local store

class LocalTDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.con = duckdb.connect(str(path))
        cols = ", ".join(f"{c} VARCHAR" for c in KEEP_COLUMNS)
        self.con.execute(f"CREATE TABLE IF NOT EXISTS sor ({cols}, received_date DATE, source_file VARCHAR)")
        self.con.execute("CREATE TABLE IF NOT EXISTS files (source_file VARCHAR PRIMARY KEY, rows BIGINT, loaded_at TIMESTAMP)")

    def status(self) -> dict[str, Any]:
        n = self.con.execute("SELECT COUNT(*), MIN(received_date), MAX(received_date), COUNT(DISTINCT platform_name) FROM sor").fetchone()
        files = self.con.execute("SELECT source_file, rows FROM files ORDER BY loaded_at DESC LIMIT 20").fetchall()
        return {"statements": n[0], "from": str(n[1]) if n[1] else None, "to": str(n[2]) if n[2] else None,
                "platforms": n[3], "recent_files": [{"file": f, "rows": r} for f, r in files]}

    def ingest_zip(self, zip_path: Path, *, platforms: list[str] | None = None, max_rows: int | None = None) -> dict[str, Any]:
        tmp = Path(tempfile.mkdtemp(prefix="dsa_"))
        loaded = 0
        csvs = 0
        try:
            for csv_path in _iter_csvs(zip_path, tmp):
                csvs += 1
                loaded += self._ingest_csv(csv_path, zip_path.name, platforms, None if max_rows is None else max_rows - loaded)
                csv_path.unlink(missing_ok=True)
                if max_rows is not None and loaded >= max_rows:
                    break
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.con.execute("INSERT OR REPLACE INTO files VALUES (?, ?, now())", [zip_path.name, loaded])
        return {"file": zip_path.name, "csv_files": csvs, "rows_loaded": loaded}

    def ingest_csv_file(self, csv_path: Path, *, platforms: list[str] | None = None) -> dict[str, Any]:
        n = self._ingest_csv(csv_path, csv_path.name, platforms, None)
        self.con.execute("INSERT OR REPLACE INTO files VALUES (?, ?, now())", [csv_path.name, n])
        return {"file": csv_path.name, "csv_files": 1, "rows_loaded": n}

    def _ingest_csv(self, csv_path: Path, source: str, platforms: list[str] | None, limit: int | None) -> int:
        desc = self.con.execute("SELECT * FROM read_csv(?, all_varchar=true, header=true, sample_size=2048) LIMIT 0",
                                [str(csv_path)]).description
        present = {d[0] for d in desc}
        if "uuid" not in present or "decision_ground" not in present:
            return 0
        sel = ", ".join(f'"{c}"' if c in present else f"NULL AS {c}" for c in KEEP_COLUMNS)
        date_expr = "TRY_CAST(SUBSTR(created_at, 1, 10) AS DATE)" if "created_at" in present else \
            "TRY_CAST(SUBSTR(application_date, 1, 10) AS DATE)"
        where = ""
        params: list[Any] = [str(csv_path)]
        if platforms and "platform_name" in present:
            where = " WHERE lower(platform_name) IN (" + ",".join("?" for _ in platforms) + ")"
            params += [p.lower() for p in platforms]
        lim = f" LIMIT {int(limit)}" if limit is not None else ""
        before = self.con.execute("SELECT COUNT(*) FROM sor").fetchone()[0]
        self.con.execute(
            f"INSERT INTO sor SELECT {sel}, {date_expr} AS received_date, '{_sql_str(source)}' AS source_file "
            f"FROM read_csv(?, all_varchar=true, header=true, sample_size=2048){where}{lim}", params)
        return self.con.execute("SELECT COUNT(*) FROM sor").fetchone()[0] - before

    # analysis ---------------------------------------------------------------
    def _where(self, platform: str | None, date_from: str | None, date_to: str | None, country: str | None,
               extra: dict[str, str] | None = None) -> tuple[str, list[Any]]:
        clauses, params = [], []
        if platform:
            clauses.append("lower(platform_name) = lower(?)")
            params.append(platform)
        if date_from:
            clauses.append("received_date >= CAST(? AS DATE)")
            params.append(date_from)
        if date_to:
            clauses.append("received_date <= CAST(? AS DATE)")
            params.append(date_to)
        if country:
            clauses.append("territorial_scope LIKE ?")
            params.append(f"%{country.upper()}%")
        for k, v in (extra or {}).items():
            if k not in KEEP_COLUMNS:
                raise ValueError(f"unknown filter field {k}")
            clauses.append(f"{k} = ?")
            params.append(v)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def group_counts(self, group_by: str, *, platform: str | None, date_from: str | None, date_to: str | None,
                     country: str | None, filters: dict[str, str] | None = None, limit: int = 50) -> list[tuple[str, int]]:
        where, params = self._where(platform, date_from, date_to, country, filters)
        if group_by in ARRAY_COLUMNS:
            expr = f"UNNEST(string_split(regexp_replace(COALESCE({group_by}, ''), '[\\[\\]\" ]', '', 'g'), ','))"
            sql = (f"SELECT v, COUNT(*) FROM (SELECT {expr} AS v FROM sor{where}) WHERE v <> '' "
                   f"GROUP BY v ORDER BY 2 DESC LIMIT {int(limit)}")
        elif group_by in KEEP_COLUMNS or group_by == "received_date":
            sql = f"SELECT CAST({group_by} AS VARCHAR), COUNT(*) FROM sor{where} GROUP BY 1 ORDER BY 2 DESC LIMIT {int(limit)}"
        else:
            raise ValueError(f"cannot group by {group_by}")
        return [(r[0], r[1]) for r in self.con.execute(sql, params).fetchall()]

    def total(self, *, platform: str | None, date_from: str | None, date_to: str | None, country: str | None,
              filters: dict[str, str] | None = None) -> int:
        where, params = self._where(platform, date_from, date_to, country, filters)
        return self.con.execute(f"SELECT COUNT(*) FROM sor{where}", params).fetchone()[0]

    def sample(self, n: int, *, platform: str | None, date_from: str | None, date_to: str | None,
               country: str | None, filters: dict[str, str] | None = None) -> list[dict[str, Any]]:
        where, params = self._where(platform, date_from, date_to, country, filters)
        cols = ["uuid", "platform_name", "received_date", "category", "decision_ground", "decision_visibility",
                "automated_detection", "automated_decision", "content_type", "territorial_scope", "source_type"]
        rows = self.con.execute(f"SELECT * FROM (SELECT {', '.join(cols)} FROM sor{where}) "
                                f"USING SAMPLE reservoir({int(n)} ROWS)", params).fetchall()
        return [dict(zip(cols, [str(v) if v is not None else None for v in r])) for r in rows]

    def platforms(self) -> list[tuple[str, int]]:
        return self.con.execute("SELECT platform_name, COUNT(*) FROM sor GROUP BY 1 ORDER BY 2 DESC").fetchall()


def _sql_str(v: str) -> str:
    return v.replace("'", "''")


def _iter_csvs(zip_path: Path, tmp: Path, depth: int = 0):
    if depth > 3:
        return
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename
            if info.is_dir():
                continue
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).name)
            if name.lower().endswith(".zip"):
                inner = tmp / f"d{depth}_{safe}"
                with zf.open(info) as src, inner.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                yield from _iter_csvs(inner, tmp, depth + 1)
                inner.unlink(missing_ok=True)
            elif name.lower().endswith(".csv"):
                out = tmp / f"d{depth}_{safe}"
                with zf.open(info) as src, out.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                yield out
