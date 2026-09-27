"""ClaimTrail sources: Google Fact Check Tools, ClaimReview markup, page date metadata, Wayback, C2PA.

* Google Fact Check Tools API ``GET https://factchecktools.googleapis.com/v1alpha1/claims:search`` with
  ``query, languageCode, maxAgeDays, pageSize, pageToken, reviewPublisherSiteFilter, key`` → ``claims[]``
  (``text, claimant, claimDate, claimReview[]{publisher{name,site}, url, title, reviewDate, textualRating,
  languageCode}``) and ``nextPageToken`` (from Google's published discovery document).
* ClaimReview: schema.org JSON-LD embedded in fact-check articles.
* C2PA: read with the official ``c2pa-python`` library (optional extra).
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup

from ..core.http import Fetcher, FetchError, FetchResult
from ..core.privacy import safe_name
from ..core.results import MissingCredential

FACTCHECK_URL = "https://factchecktools.googleapis.com/v1alpha1/claims:search"

_DATE_META = {
    "article:published_time": "published", "og:published_time": "published", "article:modified_time": "modified",
    "og:updated_time": "modified", "datepublished": "published", "date": "published", "dc.date": "published",
    "dc.date.issued": "published", "dcterms.created": "published", "dcterms.issued": "published",
    "parsely-pub-date": "published", "sailthru.date": "published", "pubdate": "published", "publishdate": "published",
    "citation_publication_date": "published", "citation_date": "published", "datemodified": "modified",
}


async def factcheck_search(fetcher: Fetcher, key: str | None, query: str, *, language: str | None = None,
                           max_age_days: int | None = None, page_size: int = 20) -> tuple[list[dict], FetchResult]:
    if not key:
        raise MissingCredential("GOOGLE_FACTCHECK_API_KEY",
                                "Enable the Fact Check Tools API in a Google Cloud project and create an API key.")
    params: dict[str, Any] = {"query": query[:500], "pageSize": min(page_size, 50), "key": key}
    if language:
        params["languageCode"] = language
    if max_age_days:
        params["maxAgeDays"] = max_age_days
    res = await fetcher.get(FACTCHECK_URL, params=params, cache_ttl=6 * 3600)
    if res.status != 200:
        raise FetchError(f"Fact Check API HTTP {res.status}", url=FACTCHECK_URL, status=res.status)
    claims = []
    for c in res.json().get("claims") or []:
        claims.append({
            "text": c.get("text"), "claimant": safe_name(c.get("claimant")) if c.get("claimant") else None,
            "claim_date": c.get("claimDate"),
            "reviews": [{"publisher": (r.get("publisher") or {}).get("name"), "site": (r.get("publisher") or {}).get("site"),
                         "url": r.get("url"), "title": r.get("title"), "review_date": r.get("reviewDate"),
                         "rating": r.get("textualRating"), "language": r.get("languageCode")}
                        for r in c.get("claimReview") or []],
        })
    return claims, res


def _walk(obj: Any, out: list[dict]) -> None:
    if isinstance(obj, list):
        for x in obj:
            _walk(x, out)
    elif isinstance(obj, dict):
        t = obj.get("@type")
        types = t if isinstance(t, list) else [t]
        if "ClaimReview" in types:
            out.append(obj)
        for k in ("@graph", "mainEntity", "itemReviewed", "review", "hasPart"):
            if k in obj:
                _walk(obj[k], out)


def extract_claimreview(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    found: list[dict] = []
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            _walk(json.loads(tag.string or tag.get_text() or ""), found)
        except (json.JSONDecodeError, ValueError):
            continue
    out = []
    for cr in found:
        item = cr.get("itemReviewed") or {}
        rating = cr.get("reviewRating") or {}
        author = cr.get("author") or {}
        if isinstance(author, list):
            author = author[0] if author else {}
        appearance = item.get("appearance") or item.get("firstAppearance") or []
        if isinstance(appearance, dict):
            appearance = [appearance]
        claimant = (item.get("author") or {}).get("name") if isinstance(item.get("author"), dict) else None
        out.append({
            "claim_reviewed": cr.get("claimReviewed"), "date_published": cr.get("datePublished"),
            "rating": rating.get("alternateName") or rating.get("ratingValue"),
            "fact_checker": author.get("name") if isinstance(author, dict) else None,
            "claimant": safe_name(claimant) if claimant else None,
            "claim_first_seen": item.get("datePublished"),
            "appearances": [a.get("url") for a in appearance if isinstance(a, dict) and a.get("url")][:10],
            "url": cr.get("url"),
        })
    return out


@dataclass
class PageDates:
    url: str
    title: str | None
    canonical: str | None
    dates: list[dict[str, str]]


def extract_page_dates(html: str, url: str, last_modified: str | None = None) -> PageDates:
    soup = BeautifulSoup(html, "lxml")
    dates: list[dict[str, str]] = []
    for m in soup.find_all("meta"):
        key = (m.get("property") or m.get("name") or m.get("itemprop") or "").strip().lower()
        if key in _DATE_META and m.get("content"):
            dates.append({"kind": _DATE_META[key], "value": m["content"].strip(), "source": f"meta {key}"})
    for tag in soup.find_all(attrs={"itemprop": re.compile("^date(Published|Created|Modified)$", re.I)}):
        v = tag.get("content") or tag.get("datetime")
        if v:
            kind = "modified" if "modified" in tag["itemprop"].lower() else "published"
            dates.append({"kind": kind, "value": v.strip(), "source": f"itemprop {tag['itemprop']}"})
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, ValueError):
            continue
        stack = [data]
        while stack:
            obj = stack.pop()
            if isinstance(obj, list):
                stack.extend(obj)
            elif isinstance(obj, dict):
                for k, kind in (("datePublished", "published"), ("dateCreated", "created"), ("uploadDate", "published"),
                                ("dateModified", "modified")):
                    if isinstance(obj.get(k), str):
                        dates.append({"kind": kind, "value": obj[k], "source": f"json-ld {k}"})
                if "@graph" in obj:
                    stack.append(obj["@graph"])
    first_time = soup.find("time", attrs={"datetime": True})
    if first_time:
        dates.append({"kind": "published?", "value": first_time["datetime"], "source": "first <time datetime>"})
    if last_modified:
        dates.append({"kind": "server_last_modified", "value": last_modified, "source": "HTTP Last-Modified"})
    canon = soup.find("link", rel="canonical")
    title = soup.title.string.strip() if soup.title and soup.title.string else None
    uniq = {(d["kind"], d["value"]): d for d in dates}
    return PageDates(url=url, title=title, canonical=canon.get("href") if canon else None, dates=list(uniq.values()))


DIGITAL_SOURCE_TYPES = {
    "trainedAlgorithmicMedia": "generated by a trained AI model",
    "compositeWithTrainedAlgorithmicMedia": "composite including AI-generated content",
    "algorithmicMedia": "generated algorithmically (not trained model)",
    "digitalCapture": "captured by a camera/recorder",
    "digitalArt": "created by a person with digital tools",
    "compositeCapture": "composite of captures",
    "composite": "composite",
    "screenCapture": "screen capture",
    "virtualRecording": "virtual recording",
    "minorHumanEdits": "minor human edits",
    "negativeFilm": "scanned negative film", "positiveFilm": "scanned positive film", "print": "scanned print",
    "humanEdits": "human edits", "algorithmicallyEnhanced": "algorithmically enhanced",
    "dataDrivenMedia": "data-driven media", "compositeSynthetic": "composite of synthetic elements",
}


def read_c2pa(data: bytes, mime: str) -> dict[str, Any]:
    try:
        import c2pa  # type: ignore[import-not-found]
    except ImportError:
        return {"status": "unavailable", "detail": "install the 'c2pa' extra: pip install 'agent-osint[c2pa]'"}
    try:
        reader = c2pa.Reader(mime, io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - c2pa raises its own hierarchy
        name = type(exc).__name__
        msg = str(exc)
        if "ManifestNotFound" in name or "no JUMBF" in msg:
            return {"status": "no_manifest", "detail": "no C2PA Content Credentials embedded"}
        if "Remote" in msg or "remote manifest" in msg:
            return {"status": "remote_manifest_unreachable", "detail": msg[:300]}
        return {"status": "error", "detail": f"{name}: {msg[:300]}"}
    try:
        store = json.loads(reader.json())
        state = reader.get_validation_state() if hasattr(reader, "get_validation_state") else store.get("validation_state")
    finally:
        try:
            reader.close()
        except Exception:  # noqa: BLE001
            pass
    active = store.get("active_manifest")
    m = (store.get("manifests") or {}).get(active) or {}
    actions = []
    for a in m.get("assertions") or []:
        if str(a.get("label", "")).startswith("c2pa.actions"):
            for act in (a.get("data") or {}).get("actions") or []:
                dst = str(act.get("digitalSourceType") or "").rsplit("/", 1)[-1] or None
                actions.append({"action": act.get("action"), "when": act.get("when"),
                                "software_agent": act.get("softwareAgent") if isinstance(act.get("softwareAgent"), str)
                                else (act.get("softwareAgent") or {}).get("name"),
                                "digital_source_type": dst,
                                "meaning": DIGITAL_SOURCE_TYPES.get(dst or "", None)})
    sig = m.get("signature_info") or {}
    ai = [a for a in actions if a["digital_source_type"] in {"trainedAlgorithmicMedia", "compositeWithTrainedAlgorithmicMedia"}]
    return {
        "status": "manifest_found", "validation_state": state,
        "validation_issues": [{"code": v.get("code"), "explanation": v.get("explanation")}
                              for v in store.get("validation_status") or []],
        "claim_generator": m.get("claim_generator") or ", ".join(
            str(g.get("name")) for g in (m.get("claim_generator_info") or []) if isinstance(g, dict)),
        "title": m.get("title"), "signer": sig.get("issuer"), "signed_at": sig.get("time"),
        "actions": actions, "declares_ai_generation": bool(ai),
        "ingredients": [i.get("title") for i in m.get("ingredients") or []][:10],
    }
