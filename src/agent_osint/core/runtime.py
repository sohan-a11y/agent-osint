"""Per-process runtime: settings, HTTP client and evidence store singletons."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass

from .config import Settings
from .evidence import EvidenceStore
from .http import Fetcher


@dataclass
class Runtime:
    settings: Settings
    fetcher: Fetcher
    evidence: EvidenceStore


_runtimes: dict[str, Runtime] = {}


def get_runtime(tool: str, settings: Settings | None = None, fetcher: Fetcher | None = None) -> Runtime:
    if tool not in _runtimes:
        s = settings or Settings.from_env()
        s.home.mkdir(parents=True, exist_ok=True)
        f = fetcher or Fetcher(s, cache_path=s.data_dir(tool) / "http_cache.sqlite")
        ev = EvidenceStore(s.data_dir(tool) / "evidence.sqlite")
        _runtimes[tool] = Runtime(settings=s, fetcher=f, evidence=ev)
    return _runtimes[tool]


def reset_runtimes() -> None:
    _runtimes.clear()


def configure_logging() -> None:
    # MCP stdio servers must never write to stdout; logs go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
