"""DepCheck: verify that dependencies an AI agent wants to install are real and not risky."""

from __future__ import annotations

import asyncio
from typing import Any

from rapidfuzz import process
from rapidfuzz.distance import DamerauLevenshtein

from ..core.http import FetchError
from ..core.results import ToolInputError, ok
from ..core.runtime import Runtime
from . import parse
from .registries import PackageInfo, Registries, age_days, load_bundled, pep503

NEW_DAYS_HIGH = 30
NEW_DAYS_MEDIUM = 90
LIMITATIONS = [
    "Registry metadata cannot prove a package is safe; it catches non-existent, brand-new, look-alike and "
    "otherwise suspicious packages. Review code for anything critical.",
]


class DepCheck:
    def __init__(self, rt: Runtime):
        self.rt = rt
        self.reg = Registries(rt.fetcher, rt.settings.data_dir("depcheck"))
        slop = load_bundled("slopsquatting_names.json")
        self.hallucinated = {pep503(k): v for k, v in slop["names"].items()}
        self.hallucinated_source = slop["source"]
        self._sem = asyncio.Semaphore(8)

    async def _lookalikes(self, eco: str, name: str) -> list[dict[str, Any]]:
        n = pep503(name) if eco == "pypi" else name.lower()
        if eco == "pypi":
            top = await self.reg.top_pypi()
            if n in set(top[:5000]):
                return []
            hits = process.extract(n, top[:15000], scorer=DamerauLevenshtein.distance, limit=5, score_cutoff=2)
            out = []
            for cand, dist, rank in hits:
                if cand == n or dist == 0:
                    continue
                if len(n) <= 4 and dist > 1:
                    continue
                out.append({"name": cand, "edit_distance": int(dist), "popularity_rank": rank + 1})
            # delimiter/affix confusions: python-foo, foo-python, pyfoo, foo-py
            for variant in {n.removeprefix("python-"), n.removesuffix("-python"), n.removeprefix("py"),
                            n.removesuffix("-py"), n.replace("-", "")}:
                if variant != n and variant in top[:5000] and all(o["name"] != variant for o in out):
                    out.append({"name": variant, "edit_distance": None, "popularity_rank": top.index(variant) + 1,
                                "pattern": "affix/delimiter variant"})
            return sorted(out, key=lambda o: o["popularity_rank"])[:5]
        top_npm = await self.reg.top_npm()
        if n in set(top_npm):
            return []
        out = []
        for cand, dist, rank in process.extract(n, top_npm, scorer=DamerauLevenshtein.distance, limit=5,
                                                score_cutoff=2):
            if cand == n or (len(n) <= 4 and dist > 1):
                continue
            out.append({"name": cand, "edit_distance": int(dist), "popularity_rank": rank + 1})
        if n.startswith("@") and "/" in n:  # scope confusion: @evil/lodash vs lodash
            bare = n.split("/", 1)[1]
            if bare in top_npm:
                out.append({"name": bare, "edit_distance": None, "popularity_rank": top_npm.index(bare) + 1,
                            "pattern": "scoped copy of a popular unscoped package"})
        return sorted(out, key=lambda o: o["popularity_rank"])[:5]

    async def check_one(self, ecosystem: str, name: str) -> dict[str, Any]:
        eco = ecosystem.lower()
        if eco not in {"pypi", "npm"}:
            raise ToolInputError("ecosystem must be 'pypi' or 'npm'")
        async with self._sem:
            info: PackageInfo = await (self.reg.pypi(name) if eco == "pypi" else self.reg.npm(name))
        risks: list[dict[str, str]] = []
        ev = self.rt.evidence.record(info.raw, note=f"{eco} registry lookup for {name}", keep_body=False) if info.raw else None
        known_hallu = self.hallucinated.get(pep503(name)) if eco == "pypi" else None
        if not info.exists:
            looks = await self._lookalikes(eco, name)
            risks.append({"level": "critical", "code": "not_found",
                          "detail": "Package does not exist on the registry. It is likely hallucinated or misspelled; "
                                    "do not install it, and do not register it — attackers pre-register such names "
                                    "(slopsquatting)."})
            if known_hallu:
                risks.append({"level": "info", "code": "known_llm_hallucination",
                              "detail": f"Name was hallucinated by {', '.join(known_hallu)} in the Trend Micro "
                                        "slopsquatting study."})
            return {"ecosystem": eco, "name": name, "exists": False, "verdict": "block", "risks": risks,
                    "did_you_mean": looks, "evidence_id": ev.id if ev else None}
        if info.security_placeholder:
            risks.append({"level": "critical", "code": "npm_security_placeholder",
                          "detail": "npm replaced this package with a security-holding placeholder, which it does "
                                    "after removing malicious packages. Do not use it."})
        age = age_days(info.first_release)
        if info.latest_version in {"0.0.0", "0.0.1"} and info.release_count <= 1 and not info.security_placeholder:
            risks.append({"level": "medium", "code": "placeholder_version",
                          "detail": f"only version is {info.latest_version}; likely an empty or test package"})
        if age is not None and age < NEW_DAYS_HIGH:
            risks.append({"level": "high", "code": "very_new", "detail": f"first published {age} days ago"})
        elif age is not None and age < NEW_DAYS_MEDIUM:
            risks.append({"level": "medium", "code": "new", "detail": f"first published {age} days ago"})
        if info.release_count <= 1 and (age is None or age < 180):
            risks.append({"level": "medium", "code": "single_release", "detail": "only one release"})
        if known_hallu:
            risks.append({"level": "high", "code": "registered_hallucinated_name",
                          "detail": f"This name did not exist when LLMs ({', '.join(known_hallu)}) hallucinated it "
                                    "(Trend Micro slopsquatting study); it is now registered — possible slopsquat."})
        looks = await self._lookalikes(eco, name)
        close = [x for x in looks if (x.get("edit_distance") or 0) <= 2]
        if close and (age is None or age < 365 or info.release_count <= 3):
            risks.append({"level": "high", "code": "lookalike_of_popular",
                          "detail": f"name resembles popular package(s) {[x['name'] for x in close]} and is new or "
                                    "has few releases — possible typosquat"})
        if not info.repository and not info.homepage:
            risks.append({"level": "low", "code": "no_source_link", "detail": "no repository or homepage URL"})
        if not (info.summary or "").strip():
            risks.append({"level": "low", "code": "no_description", "detail": "empty description"})
        if info.deprecated:
            risks.append({"level": "medium", "code": "deprecated", "detail": str(info.deprecated)[:200]})
        if info.latest_yanked:
            risks.append({"level": "medium", "code": "latest_yanked", "detail": "latest release is yanked"})
        if info.install_scripts:
            risks.append({"level": "medium", "code": "install_scripts",
                          "detail": f"runs {', '.join(info.install_scripts)} script(s) on install"})
        levels = {r["level"] for r in risks}
        codes = {r["code"] for r in risks}
        impostor = bool(codes & {"lookalike_of_popular", "registered_hallucinated_name"})
        fresh = bool(codes & {"very_new", "new", "single_release"})
        squat_like = "registered_hallucinated_name" in codes and info.release_count <= 2 and not info.repository
        if "critical" in levels or (impostor and fresh) or squat_like:
            verdict = "block"
        elif levels & {"high", "medium"}:
            verdict = "caution"
        else:
            verdict = "ok"
        return {"ecosystem": eco, "name": name, "canonical_name": info.canonical_name, "exists": True,
                "verdict": verdict, "risks": risks,
                "facts": {"first_release": info.first_release, "age_days": age, "latest_version": info.latest_version,
                          "latest_release": info.latest_release, "releases": info.release_count,
                          "repository": info.repository, "maintainers": info.maintainers,
                          "summary": (info.summary or "")[:200]},
                "similar_popular": looks[:3], "evidence_id": ev.id if ev else None}

    async def check_packages(self, ecosystem: str, names: list[str]) -> dict[str, Any]:
        names = [n.strip() for n in names if n and n.strip()]
        if not names:
            raise ToolInputError("no package names given")
        if len(names) > 100:
            raise ToolInputError("at most 100 packages per call")

        async def safe(n: str) -> dict[str, Any]:
            try:
                return await self.check_one(ecosystem, n)
            except FetchError as exc:
                return {"ecosystem": ecosystem, "name": n, "verdict": "unknown", "error": str(exc)}

        results = await asyncio.gather(*(safe(n) for n in dict.fromkeys(names)))
        summary: dict[str, int] = {}
        for r in results:
            summary[r["verdict"]] = summary.get(r["verdict"], 0) + 1
        order = {"block": 0, "unknown": 1, "caution": 2, "ok": 3}
        results.sort(key=lambda r: order.get(r["verdict"], 4))
        return ok({"summary": summary, "results": results,
                   "popularity_list": self.reg.top_source if ecosystem == "pypi" else self.reg.top_npm_source},
                  limitations=LIMITATIONS)

    async def check_imports(self, code: str, language: str, local_modules: list[str] | None = None) -> dict[str, Any]:
        lang = language.lower()
        warnings: list[str] = []
        if lang in {"python", "py"}:
            deps, warnings = parse.python_imports(code, set(local_modules or []))
            eco = "pypi"
        elif lang in {"javascript", "js", "typescript", "ts", "node"}:
            deps = parse.js_imports(code)
            eco = "npm"
        else:
            raise ToolInputError("language must be python or javascript/typescript")
        if not deps:
            return ok({"dependencies": [], "summary": {}}, warnings=warnings + ["No third-party imports found."])
        res = await self.check_packages(eco, [d["package"] for d in deps])
        by_pkg = {r["name"]: r for r in res["data"]["results"]}
        mapped = [{"import": d["import"], "package": d["package"], **{k: v for k, v in by_pkg.get(d["package"], {}).items()
                                                                       if k not in {"name"}}} for d in deps]
        notes = [f"import '{d['import']}' is provided by package '{d['package']}'" for d in deps if d["import"] != d["package"]
                 and d["import"].split(".")[0] != d["package"]]
        return ok({"ecosystem": eco, "summary": res["data"]["summary"], "dependencies": mapped, "notes": notes},
                  warnings=warnings, limitations=LIMITATIONS + ["Imports of local modules are reported as packages "
                                                                "unless listed in local_modules."])

    async def check_manifest(self, content: str, kind: str) -> dict[str, Any]:
        k = kind.lower()
        if k in {"requirements", "requirements.txt"}:
            deps, eco = parse.requirements_txt(content), "pypi"
        elif k in {"pyproject", "pyproject.toml"}:
            deps, eco = parse.pyproject_toml(content), "pypi"
        elif k in {"package_json", "package.json"}:
            deps, eco = parse.package_json(content), "npm"
        else:
            raise ToolInputError("kind must be requirements, pyproject or package_json")
        res = await self.check_packages(eco, [d["package"] for d in deps])
        return ok({"ecosystem": eco, "declared": deps, **res["data"]}, limitations=LIMITATIONS)

    async def suggest(self, ecosystem: str, name: str) -> dict[str, Any]:
        looks = await self._lookalikes(ecosystem.lower(), name)
        return ok({"query": name, "suggestions": looks, "popularity_list": self.reg.top_source})
