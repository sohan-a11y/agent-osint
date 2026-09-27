"""Live benchmark: re-check every package name that coding LLMs hallucinated in Trend Micro's
slopsquatting study (github.com/trendmicro/slopsquatting) against PyPI today.

Names that did not exist during the study but exist now are exactly what DepCheck's
`registered_hallucinated_name` rule is for.

    python scripts/slopsquat_benchmark.py [--out results.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter

from agent_osint.core.runtime import get_runtime
from agent_osint.depcheck.registries import load_bundled
from agent_osint.depcheck.service import DepCheck


async def main(out: str | None) -> None:
    names = sorted(load_bundled("slopsquatting_names.json")["names"])
    dc = DepCheck(get_runtime("depcheck"))
    results = []
    for i in range(0, len(names), 50):
        res = await dc.check_packages("pypi", names[i:i + 50])
        results += res["data"]["results"]
    await dc.rt.fetcher.aclose()
    verdicts = Counter(r["verdict"] for r in results)
    now_exist = [r for r in results if r.get("exists")]
    print(f"hallucinated names checked: {len(results)}")
    print(f"verdicts: {dict(verdicts)}")
    print(f"now registered on PyPI: {len(now_exist)}")
    for r in sorted(now_exist, key=lambda r: r["facts"]["first_release"] or ""):
        f = r["facts"]
        print(f"  {r['name']:34} first release {str(f['first_release'])[:10]}  releases={f['releases']:<3} "
              f"verdict={r['verdict']}  repo={'yes' if f['repository'] else 'no'}")
    if out:
        with open(out, "w") as fh:
            json.dump(results, fh, indent=1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    asyncio.run(main(ap.parse_args().out))
