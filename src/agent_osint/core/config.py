"""Environment-driven settings shared by every server.

All configuration comes from environment variables so that each MCP server can
be configured from the client's MCP config block without files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from agent_osint import __version__


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - configuration error path
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    home: Path
    user_agent: str
    contact_email: str | None
    http_timeout: float
    cache_ttl_seconds: float
    max_response_bytes: int
    replay_dir: Path | None
    record_dir: Path | None
    offline: bool
    # Credentials (all optional; features that need them say so when missing)
    regulations_gov_api_key: str | None = None
    dsa_tdb_token: str | None = None
    pubpeer_devkey: str | None = None
    google_factcheck_api_key: str | None = None
    meta_ad_library_token: str | None = None
    opensanctions_api_key: str | None = None
    opensanctions_url: str = "https://api.opensanctions.org"
    companies_house_api_key: str | None = None
    certspotter_api_key: str | None = None
    allow_private_network: bool = False
    file_roots: list[Path] = field(default_factory=list)
    host_rps: dict[str, float] = field(default_factory=dict)
    host_burst: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> Settings:
        home = Path(_env("AGENT_OSINT_HOME", str(Path.home() / ".agent-osint"))).expanduser()
        contact = _env("AGENT_OSINT_CONTACT")
        ua_default = f"agent-osint/{__version__} (OSINT MCP toolkit"
        ua_default += f"; mailto:{contact})" if contact else ")"
        replay = _env("AGENT_OSINT_REPLAY_DIR")
        record = _env("AGENT_OSINT_RECORD_DIR")
        host_rps: dict[str, float] = {
            # Conservative defaults derived from each provider's published limits.
            "api.regulations.gov": 1000 / 3600,  # 1,000 GET/hour per key (api.data.gov default)
            "api.crossref.org": 5.0,
            "api.openalex.org": 5.0,
            "crt.sh": 0.5,
            "web.archive.org": 1.0,
            "pubpeer.com": 1.0,
            "factchecktools.googleapis.com": 2.0,
            "graph.facebook.com": 1.0,
            "transparency.dsa.ec.europa.eu": 2.0,
            "mirrulations.s3.amazonaws.com": 5.0,
            "api.certspotter.com": 0.5,
            "api.gleif.org": 2.0,
            "api.company-information.service.gov.uk": 2.0,  # 600 req / 5 min
            "www.find-tender.service.gov.uk": 1.0,
            "api.opensanctions.org": 2.0,
            "pypi.org": 10.0,
            "registry.npmjs.org": 10.0,
            "doaj.org": 2.0,
        }
        for item in (_env("AGENT_OSINT_HOST_RPS") or "").split(","):
            if "=" in item:
                host, rate = item.split("=", 1)
                host_rps[host.strip()] = float(rate)
        # Burst sizes for APIs with hourly quotas (sustained rate still applies).
        host_burst = {"api.regulations.gov": 100, "mirrulations.s3.amazonaws.com": 20, "pypi.org": 20,
                      "registry.npmjs.org": 20, "api.crossref.org": 10, "api.openalex.org": 10,
                      "transparency.dsa.ec.europa.eu": 10}
        roots = [Path(p).expanduser().resolve() for p in (_env("AGENT_OSINT_FILE_ROOTS") or "").split(os.pathsep) if p]
        return cls(
            home=home,
            user_agent=_env("AGENT_OSINT_USER_AGENT", ua_default) or ua_default,
            contact_email=contact,
            http_timeout=_env_float("AGENT_OSINT_HTTP_TIMEOUT", 30.0),
            cache_ttl_seconds=_env_float("AGENT_OSINT_CACHE_TTL", 24 * 3600),
            max_response_bytes=int(_env_float("AGENT_OSINT_MAX_BYTES", 8 * 1024 * 1024)),
            replay_dir=Path(replay).expanduser() if replay else None,
            record_dir=Path(record).expanduser() if record else None,
            offline=_env_bool("AGENT_OSINT_OFFLINE"),
            regulations_gov_api_key=_env("REGULATIONS_GOV_API_KEY"),
            dsa_tdb_token=_env("DSA_TDB_TOKEN"),
            pubpeer_devkey=_env("PUBPEER_DEVKEY"),
            google_factcheck_api_key=_env("GOOGLE_FACTCHECK_API_KEY"),
            meta_ad_library_token=_env("META_AD_LIBRARY_TOKEN"),
            opensanctions_api_key=_env("OPENSANCTIONS_API_KEY"),
            opensanctions_url=_env("OPENSANCTIONS_URL", "https://api.opensanctions.org")
            or "https://api.opensanctions.org",
            companies_house_api_key=_env("COMPANIES_HOUSE_API_KEY"),
            certspotter_api_key=_env("CERTSPOTTER_API_KEY"),
            allow_private_network=_env_bool("AGENT_OSINT_ALLOW_PRIVATE_NETWORK"),
            file_roots=roots,
            host_rps=host_rps,
            host_burst=host_burst,
        )

    def check_file(self, path: str, suffixes: tuple[str, ...] | None = None) -> Path:
        """Resolve a local path supplied by an agent; enforce AGENT_OSINT_FILE_ROOTS and expected file types."""
        p = Path(path).expanduser().resolve()
        if suffixes and not p.name.lower().endswith(suffixes):
            raise ValueError(f"expected a file ending in {', '.join(suffixes)}: {p.name}")
        if self.file_roots and not any(p == r or r in p.parents for r in self.file_roots):
            raise PermissionError(f"{p} is outside AGENT_OSINT_FILE_ROOTS")
        return p

    def data_dir(self, tool: str) -> Path:
        path = self.home / tool
        path.mkdir(parents=True, exist_ok=True)
        return path
