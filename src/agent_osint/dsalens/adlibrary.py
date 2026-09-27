"""Meta Ad Library API (``GET https://graph.facebook.com/<version>/ads_archive``).

Parameters used (from Meta's reference): ``ad_reached_countries`` (required), ``search_terms``,
``search_page_ids``, ``ad_type``, ``ad_active_status``, ``ad_delivery_date_min/max``, ``limit``.
All ad types are returned only for ads delivered to the EU/UK; elsewhere only political/issue ads.
DSA-specific fields (e.g. ``eu_total_reach``, ``beneficiary_payers``) are requested first; if the API
rejects a field (error code 100) the request is retried with a conservative base field set.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..core.http import Fetcher, FetchError, FetchResult
from ..core.privacy import safe_name
from ..core.results import MissingCredential

BASE_FIELDS = ["id", "ad_creation_time", "ad_delivery_start_time", "ad_delivery_stop_time", "ad_snapshot_url",
               "page_id", "page_name", "bylines", "publisher_platforms", "languages", "ad_creative_bodies"]
DSA_FIELDS = ["eu_total_reach", "beneficiary_payers", "target_ages", "target_gender", "target_locations"]


class AdLibrary:
    def __init__(self, fetcher: Fetcher, token: str | None):
        self.fetcher = fetcher
        self.token = token
        self.version = os.environ.get("META_GRAPH_VERSION", "v26.0")

    async def search(self, *, countries: list[str], search_terms: str | None = None, page_ids: list[str] | None = None,
                     ad_type: str = "ALL", date_min: str | None = None, date_max: str | None = None,
                     active_status: str = "ALL", limit: int = 50, max_pages: int = 4) -> tuple[list[dict], list[FetchResult], list[str]]:
        if not self.token:
            raise MissingCredential("META_AD_LIBRARY_TOKEN", "Create a Meta developer app, complete identity "
                                    "verification at facebook.com/ID, and generate a user access token.")
        warnings: list[str] = []
        fields = BASE_FIELDS + DSA_FIELDS
        params: dict[str, Any] = {
            "ad_reached_countries": json.dumps([c.upper() for c in countries]), "ad_type": ad_type,
            "ad_active_status": active_status, "limit": min(limit, 100), "access_token": self.token,
        }
        if search_terms:
            params["search_terms"] = search_terms[:100]
        if page_ids:
            params["search_page_ids"] = json.dumps(page_ids[:10])
        if date_min:
            params["ad_delivery_date_min"] = date_min
        if date_max:
            params["ad_delivery_date_max"] = date_max
        url = f"https://graph.facebook.com/{self.version}/ads_archive"
        ads: list[dict] = []
        results: list[FetchResult] = []
        for attempt in range(2):
            params["fields"] = ",".join(fields)
            res = await self.fetcher.get(url, params=params, cache_ttl=6 * 3600)
            if res.status == 400:
                err = (res.json().get("error") or {}) if res.content else {}
                if err.get("code") == 100 and attempt == 0:
                    fields = BASE_FIELDS
                    warnings.append("DSA fields rejected by the API; retried with base fields only.")
                    continue
                raise FetchError(f"Ad Library error: {err.get('message', 'HTTP 400')}", url=url, status=400)
            if res.status != 200:
                raise FetchError(f"Ad Library HTTP {res.status}", url=url, status=res.status)
            break
        payload = res.json()
        results.append(res)
        ads += payload.get("data") or []
        nxt = (payload.get("paging") or {}).get("next")
        pages = 1
        while nxt and len(ads) < limit and pages < max_pages:
            r2 = await self.fetcher.get(nxt, cache_ttl=6 * 3600)
            if r2.status != 200:
                break
            results.append(r2)
            p2 = r2.json()
            ads += p2.get("data") or []
            nxt = (p2.get("paging") or {}).get("next")
            pages += 1
        return [clean_ad(a) for a in ads[:limit]], results, warnings


def clean_ad(ad: dict) -> dict:
    out = {k: ad.get(k) for k in BASE_FIELDS + DSA_FIELDS if k in ad}
    if "bylines" in out and out["bylines"]:
        out["bylines"] = safe_name(out["bylines"])
    payers = out.get("beneficiary_payers")
    if isinstance(payers, list):
        cleaned = []
        for p in payers:
            if isinstance(p, dict):
                cleaned.append({k: safe_name(v) if isinstance(v, str) else v for k, v in p.items()})
        out["beneficiary_payers"] = cleaned
    if "ad_snapshot_url" in out and out["ad_snapshot_url"]:
        out["ad_snapshot_url"] = out["ad_snapshot_url"].split("&access_token=")[0]
    return out
