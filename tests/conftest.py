from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_osint.core.config import Settings
from agent_osint.core.http import write_fixture
from agent_osint.core.runtime import get_runtime, reset_runtimes

FIXTURES = Path(__file__).parent / "fixtures"


class Replay:
    """Writes replay fixtures and builds a runtime that serves only from them."""

    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch):
        self.dir = tmp / "replay"
        self.dir.mkdir()
        self.home = tmp / "home"
        monkeypatch.setenv("AGENT_OSINT_HOME", str(self.home))
        monkeypatch.setenv("AGENT_OSINT_REPLAY_DIR", str(self.dir))
        for var in ("REGULATIONS_GOV_API_KEY", "DSA_TDB_TOKEN", "PUBPEER_DEVKEY", "GOOGLE_FACTCHECK_API_KEY",
                    "META_AD_LIBRARY_TOKEN", "OPENSANCTIONS_API_KEY", "COMPANIES_HOUSE_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        reset_runtimes()

    def add(self, url: str, body: Any = b"", *, status: int = 200, method: str = "GET",
            headers: dict[str, str] | None = None, json_body: Any = None, final_url: str | None = None) -> None:
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
            headers = {"content-type": "application/json", **(headers or {})}
        write_fixture(self.dir, method, url, status=status, body=body, headers=headers, json_body=json_body,
                      final_url=final_url)

    def runtime(self, tool: str, **env: str):
        settings = Settings.from_env()
        for k, v in env.items():
            setattr(settings, k, v)
        return get_runtime(tool, settings=settings)


@pytest.fixture
def replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Replay:
    r = Replay(tmp_path, monkeypatch)
    yield r
    reset_runtimes()


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
