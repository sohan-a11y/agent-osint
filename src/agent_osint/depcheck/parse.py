"""Extract dependencies from code and manifests."""

from __future__ import annotations

import ast
import json
import re
import sys

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from .registries import load_bundled

STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
NODE_BUILTINS = {
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console", "constants", "crypto", "dgram",
    "diagnostics_channel", "dns", "domain", "events", "fs", "http", "http2", "https", "inspector", "module", "net",
    "os", "path", "perf_hooks", "process", "punycode", "querystring", "readline", "repl", "stream", "string_decoder",
    "sys", "timers", "tls", "trace_events", "tty", "url", "util", "v8", "vm", "wasi", "worker_threads", "zlib", "test",
}
_ALIASES: dict[str, str] | None = None
# Namespace packages whose children are separately distributed.
NAMESPACE_PARENTS = {"google", "google.cloud", "azure", "azure.mgmt", "azure.storage", "azure.ai", "azure.keyvault",
                     "azure.monitor", "azure.search", "azure.data"}


def aliases() -> dict[str, str]:
    global _ALIASES
    if _ALIASES is None:
        _ALIASES = load_bundled("import_aliases.json")["aliases"]
    return _ALIASES


def python_imports(code: str, local_modules: set[str] | None = None) -> tuple[list[dict[str, str]], list[str]]:
    """Return [{import, package}] for third-party imports and a list of parse warnings."""
    warnings: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        warnings.append(f"could not parse Python ({exc.msg} at line {exc.lineno}); fell back to regex")
        mods = re.findall(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", code, re.MULTILINE)
        names = [a or b for a, b in mods]
    else:
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                if node.module in NAMESPACE_PARENTS:
                    # namespace packages: "from google.cloud import storage" -> google.cloud.storage
                    names += [f"{node.module}.{a.name}" for a in node.names if a.name != "*"]
                else:
                    names.append(node.module)
    local = local_modules or set()
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    al = aliases()
    for full in names:
        top = full.split(".")[0]
        if top in STDLIB or top in local or not top:
            continue
        pkg = None
        parts = full.split(".")
        for i in range(len(parts), 0, -1):  # longest alias match first (google.cloud.storage ...)
            key = ".".join(parts[:i])
            if key in al:
                pkg = al[key]
                break
        if pkg is None and parts[0] in {"google", "azure"} and len(parts) >= 2:
            # namespace packages: google.cloud.storage -> google-cloud-storage, azure.identity -> azure-identity,
            # azure.mgmt.compute -> azure-mgmt-compute
            depth = 3 if ".".join(parts[:2]) in NAMESPACE_PARENTS else 2
            pkg = "-".join(parts[:depth])
        if pkg is None:
            pkg = top
        if pkg not in seen:
            seen.add(pkg)
            out.append({"import": full if pkg != top else top, "package": pkg})
    return out, warnings


_JS_RE = re.compile(
    r"""(?:import\s+(?:[\w*{}\s,]+\s+from\s+)?|export\s+[\w*{}\s,]+\s+from\s+|require\s*\(\s*|import\s*\(\s*)['"]([^'"]+)['"]""")


def js_imports(code: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for m in _JS_RE.finditer(code):
        spec = m.group(1)
        if spec.startswith((".", "/", "node:", "http:", "https:", "data:", "#")) or "${" in spec:
            continue
        parts = spec.split("/")
        pkg = "/".join(parts[:2]) if spec.startswith("@") else parts[0]
        if pkg in NODE_BUILTINS or not pkg:
            continue
        if pkg not in seen:
            seen.add(pkg)
            out.append({"import": spec, "package": pkg})
    return out


_REQ_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$")


def requirements_txt(content: str) -> list[dict[str, str]]:
    out = []
    for raw in content.splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith(("#", "-", "git+", "http:", "https:", "file:", ".")):
            continue
        if "@" in line and "://" in line:
            line = line.split("@", 1)[0].strip()
        m = _REQ_RE.match(line)
        if m:
            out.append({"package": m.group(1), "spec": (m.group(3) or "").split(";")[0].strip()})
    return out


def pyproject_toml(content: str) -> list[dict[str, str]]:
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid pyproject.toml: {exc}") from None
    reqs: list[str] = list((data.get("project") or {}).get("dependencies") or [])
    for group in ((data.get("project") or {}).get("optional-dependencies") or {}).values():
        reqs += list(group)
    out = requirements_txt("\n".join(reqs))
    poetry = ((data.get("tool") or {}).get("poetry") or {})
    for section in ("dependencies", "dev-dependencies"):
        for name, spec in (poetry.get(section) or {}).items():
            if name.lower() != "python":
                out.append({"package": name, "spec": spec if isinstance(spec, str) else json.dumps(spec)})
    for grp in (poetry.get("group") or {}).values():
        for name, spec in (grp.get("dependencies") or {}).items():
            out.append({"package": name, "spec": spec if isinstance(spec, str) else json.dumps(spec)})
    return out


def package_json(content: str) -> list[dict[str, str]]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid package.json: {exc}") from None
    out = []
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        for name, spec in (data.get(section) or {}).items():
            if isinstance(spec, str) and spec.startswith(("file:", "link:", "workspace:", "git", "http")):
                continue
            out.append({"package": name, "spec": str(spec), "section": section})
    return out
