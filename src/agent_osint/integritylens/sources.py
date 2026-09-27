"""IntegrityLens data sources.

* Crossref REST API ``/works/{doi}``: editorial notices come from two fields — ``update-to``
  (deposited by the publisher) and ``updated-by`` (backfilled by Crossref from the Retraction Watch
  database, ``source: "retraction-watch"``). Both must be checked.
* doi.org Handle API ``/api/handles/{doi}``: ``responseCode`` 1 = DOI exists, 100 = not found.
* OpenAlex ``/works/doi:{doi}`` and ``/sources/issn:{issn}``: retraction flag, venue, DOAJ membership.
* PubPeer ``POST /v3/publications?devkey=…`` with ``{"dois": [...]}`` (≤40 per request, as in PubPeer's
  own Zotero plugin) returns ``feedbacks`` with ``id``, ``url``, ``total_comments``,
  ``last_commented_at``. Commenter pseudonyms (``users``) are never returned by this tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from ..core.http import Fetcher, FetchError, FetchResult
from ..core.results import MissingCredential

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>{}|\\^`\[\]]+)", re.IGNORECASE)

NOTICE_TYPES = {
    "retraction": "retraction",
    "removal": "removal",
    "withdrawal": "retraction",
    "expression_of_concern": "expression_of_concern",
    "correction": "correction",
    "erratum": "correction",
    "corrigendum": "correction",
}
_TITLE_PREFIX_RE = re.compile(
    r"^\s*(RETRACTED(?:\s+ARTICLE)?|RETRACTION|WITHDRAWN|EXPRESSION OF CONCERN|CORRIGENDUM)\s*:", re.IGNORECASE)
_TITLE_TYPES = {"RETRACTED": "retraction", "RETRACTED ARTICLE": "retraction", "RETRACTION": "retraction",
                "WITHDRAWN": "retraction", "EXPRESSION OF CONCERN": "expression_of_concern",
                "CORRIGENDUM": "correction"}
SEVERITY = {"retraction": 3, "removal": 3, "expression_of_concern": 2, "correction": 1}


def normalise_doi(value: str) -> str:
    v = value.strip()
    v = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", v, flags=re.IGNORECASE)
    v = re.sub(r"^doi:\s*", "", v, flags=re.IGNORECASE)
    v = v.rstrip(".,;)]}>")
    # balance a trailing ')' that belongs to the DOI, e.g. 10.1016/S0140-6736(97)11096-0
    if not DOI_RE.fullmatch(v):
        raise ValueError(f"not a DOI: {value!r}")
    return v.lower()


def extract_dois(text: str) -> list[str]:
    out = []
    for m in DOI_RE.finditer(text):
        raw = m.group(1).rstrip(".,;:")
        while raw.endswith(")") and raw.count(")") > raw.count("("):
            raw = raw[:-1]
        while raw.endswith("}") or raw.endswith("]"):
            raw = raw[:-1]
        try:
            d = normalise_doi(raw)
        except ValueError:
            continue
        if d not in out:
            out.append(d)
    return out


@dataclass
class Signal:
    kind: str  # retraction | removal | expression_of_concern | correction | pubpeer_discussion | ...
    source: str
    date: str | None = None
    detail: str | None = None
    url: str | None = None
    severity: int = 1
    evidence_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class CrossrefRecord:
    doi: str
    found: bool
    title: str | None = None
    journal: str | None = None
    publisher: str | None = None
    issns: list[str] = field(default_factory=list)
    year: int | None = None
    signals: list[Signal] = field(default_factory=list)
    raw: FetchResult | None = None


def _date(item: dict) -> str | None:
    upd = item.get("updated") or {}
    if upd.get("date-time"):
        return upd["date-time"][:10]
    parts = (upd.get("date-parts") or [[None]])[0]
    if parts and parts[0]:
        return "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts))
    return None


def analyse_crossref_message(doi: str, message: dict) -> CrossrefRecord:
    rec = CrossrefRecord(doi=doi, found=True)
    rec.title = (message.get("title") or [None])[0]
    rec.journal = (message.get("container-title") or [None])[0]
    rec.publisher = message.get("publisher")
    rec.issns = list(message.get("ISSN") or [])
    issued = (message.get("issued") or {}).get("date-parts") or [[None]]
    rec.year = issued[0][0] if issued and issued[0] else None
    seen = set()
    for field_name in ("update-to", "updated-by"):
        for item in message.get(field_name) or []:
            kind = NOTICE_TYPES.get(str(item.get("type", "")).lower())
            if not kind:
                continue
            key = (kind, (item.get("DOI") or "").lower(), _date(item))
            if key in seen:
                continue
            seen.add(key)
            src = item.get("source") or ("publisher" if field_name == "update-to" else "crossref")
            notice = item.get("DOI")
            rec.signals.append(Signal(
                kind=kind, source=f"crossref:{field_name}:{src}", date=_date(item),
                detail=item.get("label"), url=f"https://doi.org/{notice}" if notice else None,
                severity=SEVERITY[kind]))
    has_formal = {s.kind for s in rec.signals}
    if rec.title:
        m = _TITLE_PREFIX_RE.match(rec.title)
        if m:
            kind = _TITLE_TYPES[" ".join(m.group(1).upper().split())]
            if kind not in has_formal:
                rec.signals.append(Signal(kind=kind, source="crossref:title_prefix",
                                          detail=f"title starts with '{m.group(1)}'", severity=SEVERITY[kind] - 1 or 1))
    return rec


class Sources:
    def __init__(self, fetcher: Fetcher, *, pubpeer_devkey: str | None, contact: str | None):
        self.fetcher = fetcher
        self.pubpeer_devkey = pubpeer_devkey
        self.contact = contact

    async def crossref(self, doi: str) -> CrossrefRecord:
        url = f"https://api.crossref.org/works/{quote(doi, safe='')}"
        params = {"mailto": self.contact} if self.contact else None
        res = await self.fetcher.get(url, params=params, cache_ttl=3 * 86400)
        if res.status == 404:
            rec = CrossrefRecord(doi=doi, found=False)
            rec.raw = res
            return rec
        if res.status != 200:
            raise FetchError(f"Crossref HTTP {res.status}", url=res.url, status=res.status)
        payload = res.json()
        rec = analyse_crossref_message(doi, payload.get("message", payload))
        rec.raw = res
        return rec

    async def doi_registered(self, doi: str) -> tuple[bool | None, FetchResult | None]:
        try:
            res = await self.fetcher.get(f"https://doi.org/api/handles/{quote(doi, safe='/')}", cache_ttl=30 * 86400)
        except FetchError:
            return None, None
        if res.status == 404:
            return False, res
        try:
            code = res.json().get("responseCode")
        except ValueError:
            return None, res
        return (code == 1), res

    async def openalex_work(self, doi: str) -> tuple[dict | None, FetchResult | None]:
        params = {"mailto": self.contact} if self.contact else None
        res = await self.fetcher.get(f"https://api.openalex.org/works/doi:{doi}", params=params, cache_ttl=3 * 86400)
        if res.status != 200:
            return None, res
        return res.json(), res

    async def openalex_source(self, issn: str) -> tuple[dict | None, FetchResult | None]:
        params = {"mailto": self.contact} if self.contact else None
        res = await self.fetcher.get(f"https://api.openalex.org/sources/issn:{issn}", params=params, cache_ttl=7 * 86400)
        if res.status != 200:
            return None, res
        return res.json(), res

    async def openalex_source_search(self, name: str) -> tuple[list[dict], FetchResult | None]:
        params = {"search": name, "per-page": 5}
        if self.contact:
            params["mailto"] = self.contact
        res = await self.fetcher.get("https://api.openalex.org/sources", params=params, cache_ttl=7 * 86400)
        if res.status != 200:
            return [], res
        return list(res.json().get("results") or []), res

    async def pubpeer(self, dois: list[str]) -> tuple[dict[str, dict], list[FetchResult]]:
        if not self.pubpeer_devkey:
            raise MissingCredential("PUBPEER_DEVKEY", "Request a devkey from PubPeer (see https://pubpeer.com/static/faq).")
        out: dict[str, dict] = {}
        results = []
        for i in range(0, len(dois), 40):
            chunk = dois[i:i + 40]
            res = await self.fetcher.post_json(f"https://pubpeer.com/v3/publications?devkey={self.pubpeer_devkey}",
                                               {"dois": chunk}, cache_ttl=86400)
            results.append(res)
            if res.status != 200:
                raise FetchError(f"PubPeer HTTP {res.status}", url="https://pubpeer.com/v3/publications", status=res.status)
            for fb in res.json().get("feedbacks") or []:
                doi = str(fb.get("id", "")).lower()
                last = fb.get("last_commented_at") or {}
                out[doi] = {"url": fb.get("url"), "total_comments": fb.get("total_comments"),
                            "last_commented_at": (last.get("date") if isinstance(last, dict) else last)}
        return out, results
