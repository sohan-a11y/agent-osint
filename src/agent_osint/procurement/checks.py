"""Supplier checks: company registries (GLEIF, UK Companies House) and sanctions (OpenSanctions/yente).

API shapes used:
* GLEIF ``GET https://api.gleif.org/api/v1/lei-records?filter[entity.legalName]=…`` → ``data[].attributes``
  (``lei``, ``entity.legalName.name``, ``entity.legalAddress.country``, ``entity.status``,
  ``registration.initialRegistrationDate``). Note: LEI registration date ≠ incorporation date.
* Companies House ``GET /company/{number}`` and ``/search/companies?q=`` with HTTP basic auth (key as username)
  → ``date_of_creation``, ``company_status``, ``company_name``.
* OpenSanctions / yente ``POST {base}/match/default`` with ``{"queries": {"q": {"schema": "Company",
  "properties": {"name": [...], "country": [...]}}}}`` and header ``Authorization: ApiKey …`` →
  ``responses.q.results[]`` with ``id, caption, schema, score, match, datasets``.
* Local OpenSanctions ``targets.simple.csv`` (columns ``id, schema, name, aliases, countries, sanctions,
  dataset, …``) for offline screening (entity schemas only).
"""

from __future__ import annotations

import base64
import csv
import io
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any
from urllib.parse import quote

from rapidfuzz import fuzz, process

from ..core.http import Fetcher, FetchError, FetchResult, utcnow_iso

ENTITY_SCHEMAS = {"Company", "Organization", "LegalEntity", "PublicBody"}
_SUFFIX_RE = re.compile(r"\b(limited|ltd|llc|inc|plc|gmbh|ag|sa|sas|srl|bv|nv|ab|oy|as|a/s|co|company|corp|corporation)\b\.?",
                        re.IGNORECASE)


def norm_company(name: str) -> str:
    n = _SUFFIX_RE.sub(" ", name.lower())
    n = re.sub(r"[^a-z0-9à-ÿ]+", " ", n)
    return " ".join(n.split())


class Registries:
    def __init__(self, fetcher: Fetcher, companies_house_key: str | None):
        self.fetcher = fetcher
        self.ch_key = companies_house_key

    async def gleif_by_name(self, name: str) -> tuple[list[dict], FetchResult]:
        res = await self.fetcher.get("https://api.gleif.org/api/v1/lei-records",
                                     params={"filter[entity.legalName]": name, "page[size]": 5}, cache_ttl=7 * 86400)
        if res.status != 200:
            raise FetchError(f"GLEIF HTTP {res.status}", url=res.url, status=res.status)
        out = []
        for d in res.json().get("data") or []:
            a = d.get("attributes") or {}
            ent = a.get("entity") or {}
            reg = a.get("registration") or {}
            out.append({"lei": a.get("lei") or d.get("id"), "legal_name": (ent.get("legalName") or {}).get("name"),
                        "country": (ent.get("legalAddress") or {}).get("country"), "status": ent.get("status"),
                        "lei_initial_registration": reg.get("initialRegistrationDate"),
                        "creation_date": ent.get("creationDate")})
        return out, res

    def _ch_headers(self) -> dict[str, str]:
        token = base64.b64encode(f"{self.ch_key}:".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    async def companies_house(self, number: str | None = None, name: str | None = None) -> tuple[list[dict], FetchResult | None]:
        if not self.ch_key:
            return [], None
        base = "https://api.company-information.service.gov.uk"
        if number:
            res = await self.fetcher.get(f"{base}/company/{quote(number.strip().upper())}", headers=self._ch_headers(),
                                         cache_ttl=7 * 86400)
            if res.status == 404:
                return [], res
            if res.status != 200:
                raise FetchError(f"Companies House HTTP {res.status}", url=res.url, status=res.status)
            d = res.json()
            return [{"company_number": d.get("company_number"), "name": d.get("company_name"),
                     "status": d.get("company_status"), "date_of_creation": d.get("date_of_creation"),
                     "type": d.get("type")}], res
        res = await self.fetcher.get(f"{base}/search/companies", params={"q": name, "items_per_page": 5},
                                     headers=self._ch_headers(), cache_ttl=7 * 86400)
        if res.status != 200:
            raise FetchError(f"Companies House HTTP {res.status}", url=res.url, status=res.status)
        return [{"company_number": i.get("company_number"), "name": i.get("title"), "status": i.get("company_status"),
                 "date_of_creation": i.get("date_of_creation"), "type": i.get("company_type")}
                for i in res.json().get("items") or []], res


class Sanctions:
    def __init__(self, fetcher: Fetcher, base_url: str, api_key: str | None, local_db: Path):
        self.fetcher = fetcher
        self.base = base_url.rstrip("/")
        self.key = api_key
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(local_db), check_same_thread=False)
        self.conn.executescript("CREATE TABLE IF NOT EXISTS entities (id TEXT, schema TEXT, name TEXT, norm TEXT,"
                                " countries TEXT, datasets TEXT, sanctions TEXT);"
                                "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);")
        self.conn.commit()
        self._choices: list[str] | None = None

    @property
    def remote_enabled(self) -> bool:
        # The hosted API requires a key; a self-hosted yente (custom OPENSANCTIONS_URL) may not.
        return bool(self.key) or "api.opensanctions.org" not in self.base

    def local_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]

    def load_local(self, path: str) -> dict[str, Any]:
        p = Path(path).expanduser()
        if not p.is_file():
            raise ValueError(f"file not found: {path}")
        reader = csv.DictReader(io.StringIO(p.read_text(encoding="utf-8", errors="replace")))
        rows = []
        for r in reader:
            if r.get("schema") not in ENTITY_SCHEMAS:
                continue
            names = [r.get("name") or ""] + [a for a in (r.get("aliases") or "").split(";") if a]
            for n in names:
                if n.strip():
                    rows.append((r.get("id"), r.get("schema"), n.strip(), norm_company(n), r.get("countries"),
                                 r.get("dataset") or r.get("datasets"), (r.get("sanctions") or "")[:500]))
        with self._lock:
            self.conn.execute("DELETE FROM entities")
            self.conn.executemany("INSERT INTO entities VALUES (?,?,?,?,?,?,?)", rows)
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('loaded', ?)", (f"{p.name} {utcnow_iso()}",))
            self.conn.commit()
        self._choices = None
        return {"entity_names_loaded": len(rows)}

    def match_local(self, name: str, country: str | None, threshold: int = 92) -> list[dict]:
        if self._choices is None:
            self._rows = self.conn.execute("SELECT id, schema, name, norm, countries, datasets, sanctions FROM entities").fetchall()
            self._choices = [r[3] for r in self._rows]
        if not self._choices:
            return []
        q = norm_company(name)
        if len(q) < 3:
            return []
        hits = process.extract(q, self._choices, scorer=fuzz.token_sort_ratio, limit=10, score_cutoff=threshold)
        out = []
        seen_ids: set[str] = set()
        for _choice, score, idx in hits:
            r = self._rows[idx]
            if r[0] in seen_ids:  # several names/aliases of one entity
                continue
            seen_ids.add(r[0])
            countries = (r[4] or "").lower().split(";")
            if country and r[4] and country.lower() not in countries:
                score -= 10
                if score < threshold:
                    continue
            out.append({"id": r[0], "schema": r[1], "name": r[2], "score": round(score / 100, 3), "countries": r[4],
                        "datasets": r[5], "sanctions": r[6], "source": "opensanctions_local",
                        "url": f"https://www.opensanctions.org/entities/{r[0]}/"})
        return out

    async def match_remote(self, name: str, country: str | None) -> tuple[list[dict], FetchResult]:
        props: dict[str, list[str]] = {"name": [name]}
        if country:
            props["country"] = [country.lower()]
        headers = {"Authorization": f"ApiKey {self.key}"} if self.key else {}
        res = await self.fetcher.post_json(f"{self.base}/match/default",
                                           {"queries": {"q": {"schema": "Company", "properties": props}}},
                                           headers=headers, cache_ttl=86400)
        if res.status != 200:
            raise FetchError(f"OpenSanctions HTTP {res.status}", url=f"{self.base}/match/default", status=res.status)
        results = ((res.json().get("responses") or {}).get("q") or {}).get("results") or []
        return [{"id": r.get("id"), "schema": r.get("schema"), "name": r.get("caption"), "score": r.get("score"),
                 "match": r.get("match"), "datasets": r.get("datasets"), "source": "opensanctions_api",
                 "url": f"https://www.opensanctions.org/entities/{r.get('id')}/"}
                for r in results if r.get("schema") in ENTITY_SCHEMAS], res
