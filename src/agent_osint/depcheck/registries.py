"""Package registry lookups (PyPI JSON API, npm registry) and popularity lists.

* PyPI: ``GET https://pypi.org/pypi/<name>/json`` → ``info`` and ``releases`` (files carry
  ``upload_time_iso_8601`` and ``yanked``). 404 = no such project.
* npm: ``GET https://registry.npmjs.org/<name>`` (scoped names encode ``/`` as ``%2F``) → ``time.created``,
  ``versions``, ``dist-tags.latest``, ``maintainers``; the latest manifest carries ``deprecated`` and
  ``scripts``. ``GET /-/v1/search?text=`` is used for "did you mean" suggestions.
* Popular PyPI names: hugovk/top-pypi-packages (monthly snapshot of the most-downloaded projects), fetched at
  runtime and cached; not redistributed with this package.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..core.http import Fetcher, FetchError, FetchResult

TOP_PYPI_URL = "https://raw.githubusercontent.com/hugovk/top-pypi-packages/main/top-pypi-packages.min.json"
# wooorm/npm-high-impact (MIT): npm packages with >=1M weekly downloads, ordered by downloads.
TOP_NPM_URL = "https://raw.githubusercontent.com/wooorm/npm-high-impact/main/lib/top-download.js"
FALLBACK_POPULAR_NPM = """react react-dom express lodash axios chalk commander debug semver typescript webpack
vue next moment uuid dotenv body-parser cors jsonwebtoken mongoose mysql pg redis socket.io ws yargs
inquirer fs-extra glob rimraf mkdirp minimist request node-fetch cross-env eslint prettier jest mocha
chai sinon babel-core @babel/core @babel/preset-env @types/node tslib rxjs zod date-fns dayjs classnames
prop-types styled-components tailwindcss postcss autoprefixer vite rollup esbuild nodemon bcrypt
bcryptjs passport multer nodemailer sharp puppeteer playwright cheerio jquery bootstrap""".split()

# Fallback list if the popularity snapshot cannot be fetched (well-known projects only).
FALLBACK_POPULAR_PYPI = """boto3 botocore urllib3 requests setuptools certifi charset-normalizer idna typing-extensions
python-dateutil packaging s3transfer six aiobotocore numpy pyyaml grpcio-status s3fs fsspec pip cryptography
cffi pycparser attrs google-api-core protobuf pydantic pydantic-core wheel rsa pandas pyasn1 click jmespath
platformdirs importlib-metadata zipp markupsafe jinja2 pytz filelock pyjwt tomli pluggy colorama wrapt
awscli virtualenv pytest pyparsing googleapis-common-protos cachetools pyasn1-modules google-auth sqlalchemy
aiohttp multidict yarl frozenlist aiosignal docutils psutil jsonschema tzdata greenlet requests-oauthlib oauthlib
anyio sniffio h11 httpx httpcore scipy pillow openpyxl et-xmlfile tqdm werkzeug flask itsdangerous decorator
exceptiongroup iniconfig soupsieve beautifulsoup4 lxml grpcio isodate pyarrow tomlkit regex matplotlib kiwisolver
cycler fonttools contourpy networkx sympy mpmath scikit-learn joblib threadpoolctl torch torchvision transformers
tokenizers huggingface-hub safetensors fastapi starlette uvicorn gunicorn django djangorestframework celery kombu
billiard redis pymongo psycopg2 psycopg2-binary mysqlclient pymysql alembic mako boto rich pygments markdown-it-py
mdurl typer shellingham coverage mock black isort flake8 mypy mypy-extensions pylint ruff pre-commit nodeenv
identify cfgv distlib poetry poetry-core build twine keyring pkginfo readme-renderer nh3 rfc3986 openai anthropic
langchain langchain-core tiktoken tenacity backoff opencv-python opencv-python-headless scikit-image imageio
seaborn plotly dash statsmodels patsy xgboost lightgbm catboost tensorflow keras jax jaxlib sentencepiece
nltk spacy gensim sentence-transformers datasets accelerate peft bitsandbytes selenium playwright pyppeteer
paramiko pynacl bcrypt python-dotenv python-multipart email-validator orjson ujson simplejson msgpack
websocket-client websockets pyzmq tornado twisted gevent eventlet uvloop dnspython pytest-cov pytest-mock
pytest-asyncio pytest-xdist hypothesis faker freezegun responses requests-mock moto sqlparse sqlglot duckdb
polars numba llvmlite cython pybind11 cmake ninja streamlit gradio jupyter notebook ipython ipykernel
jupyterlab nbformat nbconvert traitlets tzlocal arrow pendulum babel pyopenssl google-cloud-storage
google-cloud-bigquery google-cloud-core azure-core azure-storage-blob azure-identity msal kubernetes docker
ansible fabric invoke pyserial pywin32 pyobjc""".split()


@dataclass
class PackageInfo:
    ecosystem: str
    name: str
    exists: bool
    canonical_name: str | None = None
    first_release: str | None = None
    latest_version: str | None = None
    latest_release: str | None = None
    release_count: int = 0
    summary: str | None = None
    repository: str | None = None
    homepage: str | None = None
    maintainers: int | None = None
    deprecated: str | None = None
    latest_yanked: bool = False
    install_scripts: list[str] = field(default_factory=list)
    security_placeholder: bool = False
    raw: FetchResult | None = None


def pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _days_since(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days


class Registries:
    def __init__(self, fetcher: Fetcher, data_dir: Path):
        self.fetcher = fetcher
        self.data_dir = data_dir
        self._top_pypi: list[str] | None = None
        self._top_npm: list[str] | None = None
        self.top_source = "builtin fallback list"
        self.top_npm_source = "builtin fallback list"

    async def pypi(self, name: str) -> PackageInfo:
        norm = pep503(name)
        res = await self.fetcher.get(f"https://pypi.org/pypi/{quote(norm)}/json", cache_ttl=6 * 3600,
                                     max_bytes=32 * 1024 * 1024)
        if res.status == 404:
            return PackageInfo("pypi", name, False, raw=res)
        if res.status != 200:
            raise FetchError(f"PyPI HTTP {res.status}", url=res.url, status=res.status)
        data = res.json()
        info = data.get("info") or {}
        releases = data.get("releases") or {}
        times = []
        for files in releases.values():
            ts = [f.get("upload_time_iso_8601") for f in files or [] if f.get("upload_time_iso_8601")]
            if ts:
                times.append(min(ts))
        urls = {k.lower(): v for k, v in (info.get("project_urls") or {}).items()}
        repo = next((v for k, v in urls.items() if any(x in k for x in ("source", "repository", "code", "github"))), None)
        if not repo:
            repo = next((v for v in urls.values() if "github.com" in v or "gitlab.com" in v), None)
        latest = info.get("version")
        latest_files = releases.get(latest) or []
        latest_times = [f.get("upload_time_iso_8601") for f in latest_files if f.get("upload_time_iso_8601")]
        return PackageInfo(
            "pypi", name, True, canonical_name=info.get("name"), first_release=min(times) if times else None,
            latest_version=latest, latest_release=max(latest_times) if latest_times else None,
            release_count=sum(1 for v in releases.values() if v), summary=info.get("summary"),
            repository=repo, homepage=info.get("home_page") or urls.get("homepage"),
            latest_yanked=bool(latest_files) and all(f.get("yanked") for f in latest_files), raw=res,
        )

    async def npm(self, name: str) -> PackageInfo:
        enc = name.replace("/", "%2F") if name.startswith("@") else quote(name)
        res = await self.fetcher.get(f"https://registry.npmjs.org/{enc}", cache_ttl=6 * 3600,
                                     max_bytes=64 * 1024 * 1024, headers={"Accept": "application/json"})
        if res.status == 404:
            return PackageInfo("npm", name, False, raw=res)
        if res.status != 200:
            raise FetchError(f"npm HTTP {res.status}", url=res.url, status=res.status)
        data = res.json()
        t = data.get("time") or {}
        if "unpublished" in t:
            return PackageInfo("npm", name, False, deprecated="unpublished", raw=res)
        latest = (data.get("dist-tags") or {}).get("latest")
        manifest = (data.get("versions") or {}).get(latest) or {}
        scripts = manifest.get("scripts") or {}
        repo = data.get("repository") or manifest.get("repository")
        if isinstance(repo, dict):
            repo = repo.get("url")
        return PackageInfo(
            "npm", name, True, canonical_name=data.get("name"), first_release=t.get("created"),
            latest_version=latest, latest_release=t.get(latest) if latest else None,
            release_count=len(data.get("versions") or {}), summary=data.get("description"),
            repository=repo, homepage=data.get("homepage"), maintainers=len(data.get("maintainers") or []),
            deprecated=manifest.get("deprecated"),
            install_scripts=[k for k in ("preinstall", "install", "postinstall") if k in scripts],
            security_placeholder=str(latest or "").endswith("-security") or "npm/security-holder" in str(repo or ""),
            raw=res,
        )

    async def npm_search(self, text: str, size: int = 8) -> list[str]:
        try:
            res = await self.fetcher.get("https://registry.npmjs.org/-/v1/search", params={"text": text, "size": size},
                                         cache_ttl=86400)
        except FetchError:
            return []
        if res.status != 200:
            return []
        return [o["package"]["name"] for o in res.json().get("objects") or [] if o.get("package")]

    async def top_pypi(self) -> list[str]:
        if self._top_pypi is not None:
            return self._top_pypi
        cache = self.data_dir / "top-pypi.json"
        names: list[str] | None = None
        if cache.exists() and time.time() - cache.stat().st_mtime < 14 * 86400:
            names = json.loads(cache.read_text())
            self.top_source = "hugovk/top-pypi-packages (cached)"
        else:
            try:
                res = await self.fetcher.get(TOP_PYPI_URL, cache_ttl=7 * 86400, max_bytes=16 * 1024 * 1024)
                if res.status == 200:
                    names = [pep503(r["project"]) for r in res.json().get("rows", [])]
                    cache.write_text(json.dumps(names))
                    self.top_source = "hugovk/top-pypi-packages"
            except (FetchError, ValueError, KeyError):
                names = None
        if not names:
            names = [pep503(n) for n in FALLBACK_POPULAR_PYPI]
            self.top_source = "builtin fallback list"
        self._top_pypi = names
        return names


async def _top_npm_impl(self: Registries) -> list[str]:
    if self._top_npm is not None:
        return self._top_npm
    cache = self.data_dir / "top-npm.json"
    names: list[str] | None = None
    if cache.exists() and time.time() - cache.stat().st_mtime < 14 * 86400:
        names = json.loads(cache.read_text())
        self.top_npm_source = "wooorm/npm-high-impact (cached)"
    else:
        try:
            res = await self.fetcher.get(TOP_NPM_URL, cache_ttl=7 * 86400, max_bytes=8 * 1024 * 1024)
            if res.status == 200:
                names = re.findall(r"^\s*'([^']+)',?\s*$", res.text(), re.MULTILINE)
                if names:
                    cache.write_text(json.dumps(names))
                    self.top_npm_source = "wooorm/npm-high-impact"
        except FetchError:
            names = None
    if not names:
        names = FALLBACK_POPULAR_NPM
        self.top_npm_source = "builtin fallback list"
    self._top_npm = names
    return names


Registries.top_npm = _top_npm_impl  # type: ignore[attr-defined]


def load_bundled(name: str) -> dict[str, Any]:
    return json.loads(resources.files("agent_osint.depcheck").joinpath("data", name).read_text(encoding="utf-8"))


def age_days(iso: str | None) -> int | None:
    return _days_since(iso)
