"""DSA-Lens service: platform content-moderation statistics and ad-repository access."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any

from ..core.results import ToolInputError, ok
from ..core.runtime import Runtime
from .adlibrary import AdLibrary
from .tdb import AGG_FIELDS, KEEP_COLUMNS, LocalTDB, ResearchAPI, build_query

LIMITATIONS = [
    "Statements of reasons are self-reported by platforms; volumes reflect reporting practice as much as moderation.",
    "The Research API only covers the last 6 months; older periods require locally ingested daily dumps.",
]
GROUPABLE = sorted(set(KEEP_COLUMNS) - {"uuid", "created_at", "application_date", "content_date", "platform_uid"}
                   | {"received_date"})
API_GROUP_MAP = {"decision_visibility": "decision_visibility_single", "content_type": "content_type_single"}


def _dates(date_from: str, date_to: str, max_days: int = 62) -> list[str]:
    try:
        a, b = date.fromisoformat(date_from), date.fromisoformat(date_to)
    except ValueError as exc:
        raise ToolInputError("dates must be YYYY-MM-DD") from exc
    if b < a:
        raise ToolInputError("date_to is before date_from")
    n = (b - a).days + 1
    if n > max_days:
        raise ToolInputError(f"range too long for the API route ({n} days > {max_days})",
                             hint="Use a shorter range, or ingest dumps and use source='local'.")
    return [(a + timedelta(days=i)).isoformat() for i in range(n)]


class DSALens:
    def __init__(self, rt: Runtime):
        self.rt = rt
        self.api = ResearchAPI(rt.fetcher, rt.settings.dsa_tdb_token)
        self.local = LocalTDB(rt.settings.data_dir("dsalens") / "sor.duckdb")
        self.ads = AdLibrary(rt.fetcher, rt.settings.meta_ad_library_token)

    def _source(self, source: str) -> str:
        if source == "auto":
            return "api" if self.api.token else "local"
        if source not in {"api", "local"}:
            raise ToolInputError("source must be auto, api or local")
        return source

    # ------------------------------------------------------------ moderation stats
    async def moderation_stats(self, *, group_by: str, date_from: str, date_to: str, platform: str | None = None,
                               country: str | None = None, source: str = "auto", limit: int = 30) -> dict[str, Any]:
        src = self._source(source)
        if src == "local":
            if group_by not in GROUPABLE:
                raise ToolInputError(f"group_by must be one of {GROUPABLE}")
            status = self.local.status()
            if not status["statements"]:
                raise ToolInputError("no local statements ingested",
                                     hint="Run ingest_dump (or the CLI) with daily dump files, or set DSA_TDB_TOKEN.")
            rows = self.local.group_counts(group_by, platform=platform, date_from=date_from, date_to=date_to,
                                           country=country, limit=limit)
            total = self.local.total(platform=platform, date_from=date_from, date_to=date_to, country=country)
            return ok({"source": "local", "group_by": group_by, "total_statements": total,
                       "groups": [{"value": v, "count": c, "share": round(c / total, 4) if total else 0} for v, c in rows],
                       "coverage": {"from": status["from"], "to": status["to"]}},
                      limitations=LIMITATIONS + (["Array fields (e.g. territorial_scope) count a statement once per value."]
                                                 if group_by in {"territorial_scope", "content_type", "decision_visibility",
                                                                 "category_specification"} else []))
        # API route (aggregates endpoint, one GET per day)
        if country:
            raise ToolInputError("country filtering is not available through the aggregates API",
                                 hint="Use source='local' with ingested dumps, or count_statements with a query.")
        field = API_GROUP_MAP.get(group_by, group_by)
        if field not in AGG_FIELDS:
            raise ToolInputError(f"API group_by must be one of {sorted(AGG_FIELDS)}")
        fields = ["platform_id", field] if field != "platform_id" else ["platform_id"]
        counts: Counter[str] = Counter()
        evidence = []
        platform_names: set[str] = set()
        for day in _dates(date_from, date_to):
            data, res = await self.api.aggregates(day, fields)
            evidence.append(self.rt.evidence.record(res, note=f"DSA TDB aggregates {day}", keep_body=True))
            for row in data.get("aggregates") or []:
                pname = str(row.get("platform_name") or row.get("platform_id"))
                platform_names.add(pname)
                if platform and pname.lower() != platform.lower() and str(row.get("platform_id")) != str(platform):
                    continue
                key = str(row.get(field)) if field != "platform_id" else pname
                counts[key] += int(row.get("total") or 0)
        total = sum(counts.values())
        warnings = []
        if platform and not total:
            warnings.append(f"No statements matched platform {platform!r}; known platforms in range: "
                            f"{sorted(platform_names)[:20]}")
        return ok({"source": "api", "group_by": field, "total_statements": total,
                   "groups": [{"value": v, "count": c, "share": round(c / total, 4) if total else 0}
                              for v, c in counts.most_common(limit)]},
                  evidence=evidence[:5], warnings=warnings, limitations=LIMITATIONS)

    async def compare_platforms(self, *, date_from: str, date_to: str, platforms: list[str] | None = None,
                                source: str = "auto", top: int = 15) -> dict[str, Any]:
        """Per-platform totals, automation rates, decision grounds and top categories."""
        src = self._source(source)
        table: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
        evidence = []
        if src == "api":
            for day in _dates(date_from, date_to, max_days=31):
                for field in ("automated_decision", "decision_ground", "category", "automated_detection"):
                    data, res = await self.api.aggregates(day, ["platform_id", field])
                    evidence.append(self.rt.evidence.record(res, note=f"DSA TDB aggregates {day} {field}"))
                    for row in data.get("aggregates") or []:
                        pname = str(row.get("platform_name") or row.get("platform_id"))
                        table[pname][field][str(row.get(field))] += int(row.get("total") or 0)
        else:
            names = platforms or [p for p, _ in self.local.platforms()[:top]]
            for pname in names:
                for field in ("automated_decision", "decision_ground", "category", "automated_detection"):
                    for v, c in self.local.group_counts(field, platform=pname, date_from=date_from, date_to=date_to,
                                                        country=None, limit=100):
                        table[pname][field][str(v)] += c
        rows = []
        for pname, fields in table.items():
            if platforms and pname.lower() not in {p.lower() for p in platforms}:
                continue
            total = sum(fields["decision_ground"].values()) or sum(fields["automated_decision"].values())
            if not total:
                continue
            ad = fields["automated_decision"]
            det = fields["automated_detection"]
            rows.append({
                "platform": pname, "statements": total,
                "fully_automated_decision_share": round(ad.get("AUTOMATED_DECISION_FULLY", 0) / total, 4),
                "not_automated_decision_share": round(ad.get("AUTOMATED_DECISION_NOT_AUTOMATED", 0) / total, 4),
                "automated_detection_share": round(sum(v for k, v in det.items() if k.lower() in {"true", "yes", "1"}) / total, 4),
                "illegal_content_ground_share": round(fields["decision_ground"].get("DECISION_GROUND_ILLEGAL_CONTENT", 0) / total, 4),
                "top_categories": fields["category"].most_common(3),
            })
        rows.sort(key=lambda r: -r["statements"])
        return ok({"source": src, "date_from": date_from, "date_to": date_to, "platforms": rows[:top]},
                  evidence=evidence[:3], limitations=LIMITATIONS)

    async def count_statements(self, *, date_from: str | None = None, date_to: str | None = None,
                               platform_id: int | None = None, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        q = build_query(platform_id=platform_id, date_from=date_from, date_to=date_to, terms=filters)
        n, res = await self.api.count(q)
        ev = self.rt.evidence.record(res, note="DSA TDB /count")
        return ok({"count": n, "query": q}, evidence=[ev], limitations=LIMITATIONS)

    async def sample_statements(self, *, n: int = 10, date_from: str | None = None, date_to: str | None = None,
                                platform: str | None = None, platform_id: int | None = None, country: str | None = None,
                                filters: dict[str, str] | None = None, source: str = "auto") -> dict[str, Any]:
        n = max(1, min(n, 100))
        src = self._source(source)
        if src == "local":
            rows = self.local.sample(n, platform=platform, date_from=date_from, date_to=date_to, country=country,
                                     filters=filters)
            return ok({"source": "local", "statements": rows},
                      limitations=["Look statements up by UUID on transparency.dsa.ec.europa.eu."])
        fields = ["uuid", "platform_name", "received_date", "category", "decision_ground", "decision_visibility",
                  "automated_detection", "automated_decision", "content_type", "territorial_scope", "source_type"]
        q = build_query(platform_id=platform_id, date_from=date_from, date_to=date_to, terms=filters)
        data, res = await self.api.search(q, n, fields)
        hits = (((data.get("data") or {}).get("hits") or {}).get("hits")) or []
        ev = self.rt.evidence.record(res, note="DSA TDB /search")
        return ok({"source": "api", "statements": [h.get("_source") for h in hits]}, evidence=[ev])

    def ingest_dump(self, path: str, *, platforms: list[str] | None = None, max_rows: int | None = None) -> dict[str, Any]:
        p = self.rt.settings.check_file(path, (".zip", ".csv"))
        if not p.is_file():
            raise ToolInputError(f"file not found: {path}",
                                 hint="Download a daily dump from https://transparency.dsa.ec.europa.eu/data-download "
                                      "(per-platform 'light' files are smallest).")
        if p.suffix.lower() == ".zip":
            res = self.local.ingest_zip(p, platforms=platforms, max_rows=max_rows)
        elif p.suffix.lower() == ".csv":
            res = self.local.ingest_csv_file(p, platforms=platforms)
        else:
            raise ToolInputError("expected a .zip dump or a .csv file")
        warnings = [] if res["rows_loaded"] else ["No statement rows found (unexpected columns?)."]
        return ok({**res, "store": self.local.status()}, warnings=warnings)

    def dataset_status(self) -> dict[str, Any]:
        return ok({"research_api": "configured" if self.api.token else "not configured (DSA_TDB_TOKEN)",
                   "ad_library": "configured" if self.ads.token else "not configured (META_AD_LIBRARY_TOKEN)",
                   "local": self.local.status(), "local_platforms": self.local.platforms()[:30]})

    # ------------------------------------------------------------ ad library
    async def ad_search(self, *, countries: list[str], search_terms: str | None = None,
                        page_ids: list[str] | None = None, ad_type: str = "ALL", date_min: str | None = None,
                        date_max: str | None = None, limit: int = 50) -> dict[str, Any]:
        if not countries:
            raise ToolInputError("at least one country code is required (e.g. ['DE'])")
        ads, results, warnings = await self.ads.search(countries=countries, search_terms=search_terms,
                                                       page_ids=page_ids, ad_type=ad_type, date_min=date_min,
                                                       date_max=date_max, limit=limit)
        evidence = [self.rt.evidence.record(r, note="Meta Ad Library ads_archive", keep_body=False) for r in results]
        eu = {c.upper() for c in countries} & {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
                                                  "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK",
                                                  "SI", "ES", "SE", "GB"}
        if not eu and ad_type == "ALL":
            warnings.append("Outside the EU/UK the Ad Library only returns political/issue ads.")
        return ok({"ads": ads, "returned": len(ads)}, evidence=evidence, warnings=warnings,
                  limitations=["Payer/beneficiary names that look like private individuals are withheld."])

    async def advertiser_summary(self, *, page_id: str, countries: list[str], date_min: str | None = None,
                                 date_max: str | None = None) -> dict[str, Any]:
        ads, results, warnings = await self.ads.search(countries=countries, page_ids=[page_id], date_min=date_min,
                                                       date_max=date_max, limit=400, max_pages=8)
        if not ads:
            return ok({"page_id": page_id, "ads": 0}, warnings=warnings + ["No ads found for this page and countries."])
        platforms: Counter[str] = Counter()
        payers: Counter[str] = Counter()
        langs: Counter[str] = Counter()
        reach = 0
        starts = sorted(a.get("ad_delivery_start_time") for a in ads if a.get("ad_delivery_start_time"))
        for a in ads:
            for p in a.get("publisher_platforms") or []:
                platforms[p] += 1
            for lang in a.get("languages") or []:
                langs[lang] += 1
            for bp in a.get("beneficiary_payers") or []:
                payers[f"{bp.get('beneficiary')} / {bp.get('payer')}"] += 1
            if isinstance(a.get("eu_total_reach"), (int, float)):
                reach += int(a["eu_total_reach"])
        evidence = [self.rt.evidence.record(r, note="Meta Ad Library ads_archive", keep_body=False) for r in results]
        return ok({"page_id": page_id, "page_name": ads[0].get("page_name"), "ads": len(ads),
                   "first_delivery": starts[0] if starts else None, "last_delivery_start": starts[-1] if starts else None,
                   "publisher_platforms": platforms.most_common(), "languages": langs.most_common(10),
                   "beneficiary_payer_pairs": payers.most_common(10), "eu_total_reach_sum": reach or None},
                  evidence=evidence[:3], warnings=warnings)
