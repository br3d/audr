#!/usr/bin/env python3
"""Emit the dependency license tables used by `docs/third-party.md`.

Run with the backend virtualenv interpreter so that installed distribution
metadata is readable:

    backend/.venv/bin/python scripts/gen_third_party.py

The backend table is the *runtime* closure only: the release image builds with
`uv sync --frozen --no-dev --no-install-project`, so dev/test dependencies are
not shipped. Versions come from `backend/uv.lock` (the authoritative pin);
license strings come from the installed metadata of the same versions.

The frontend table is the `dependencies` closure from
`frontend/package-lock.json` — the packages whose code can end up in the built
SPA bundle. `devDependencies` are build tooling and are listed separately in
the document.

This script prints markdown tables to stdout for manual splicing; it does not
rewrite the document, because the surrounding prose carries judgement calls
(copyleft notes, unpinned apt packages) that should not be regenerated blindly.
"""

from __future__ import annotations

import importlib.metadata as md
import json
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# PyPI metadata predates SPDX expressions for many packages; normalise the
# legacy classifier strings to the SPDX identifier the upstream project
# actually uses.
SPDX_FIXUPS = {
    "MIT License": "MIT",
    "BSD License": "BSD-3-Clause",
    "Apache Software License": "Apache-2.0",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "Apache Software License; MIT License": "MIT OR Apache-2.0",
    "MIT License; Apache Software License": "MIT OR Apache-2.0",
    "BSD License; Apache Software License": "Apache-2.0 OR BSD-3-Clause",
    "MIT License; Apache Software License 2.0": "MIT OR Apache-2.0",
}

# Packages whose declared metadata is empty or misleading.
MANUAL = {
    "tzdata": "Apache-2.0 (bundles the IANA time zone database, public domain)",
}


def _canon(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def _installed_licenses() -> dict[str, str]:
    out: dict[str, str] = {}
    for dist in md.distributions():
        meta = dist.metadata
        name = meta.get("Name")
        if not name:
            continue
        value = meta.get("License-Expression") or ""
        if not value:
            classifiers = [
                c for c in (meta.get_all("Classifier") or []) if c.startswith("License ::")
            ]
            value = "; ".join(c.split("::")[-1].strip() for c in classifiers)
        if not value:
            raw = (meta.get("License") or "").strip().splitlines()
            value = raw[0][:60] if raw else ""
        out[_canon(name)] = SPDX_FIXUPS.get(value, value)
    return out


def backend_table() -> list[str]:
    lock = tomllib.loads((ROOT / "backend" / "uv.lock").read_text())
    packages = {p["name"]: p for p in lock["package"]}
    root = packages["audr"]

    closure: set[str] = set()

    def walk(name: str) -> None:
        if name in closure or name == "audr":
            return
        closure.add(name)
        pkg = packages.get(name)
        if pkg is None:
            return
        for dep in pkg.get("dependencies", []):
            walk(dep["name"])

    for dep in root.get("dependencies", []):
        walk(dep["name"])
    # Extras requested in backend/pyproject.toml pull further runtime packages.
    for name, extras in [("uvicorn", ["standard"]), ("psycopg", ["binary"])]:
        for extra in extras:
            for dep in (packages[name].get("optional-dependencies") or {}).get(extra, []):
                walk(dep["name"])

    direct = {dep["name"] for dep in root.get("dependencies", [])}
    licenses = _installed_licenses()

    rows = []
    for name in sorted(closure):
        lic = MANUAL.get(name) or licenses.get(_canon(name)) or "see package metadata"
        kind = "direct" if name in direct else "transitive"
        rows.append(f"| `{name}` | {packages[name]['version']} | {lic} | {kind} |")
    return rows


_NODE_SCRIPT = r"""
const fs = require("fs"), path = require("path");
const lock = JSON.parse(fs.readFileSync("package-lock.json"));
const pkgs = lock.packages;
const pj = JSON.parse(fs.readFileSync("package.json"));
function resolve(from, name) {
  let cand = from ? from + "/node_modules/" + name : "node_modules/" + name;
  for (;;) {
    if (pkgs[cand]) return cand;
    const i = cand.lastIndexOf("node_modules/", cand.length - ("node_modules/" + name).length - 1);
    if (i < 0) break;
    cand = cand.slice(0, i) + "node_modules/" + name;
  }
  return pkgs["node_modules/" + name] ? "node_modules/" + name : null;
}
const seen = new Set();
function walk(p) {
  if (!p || seen.has(p)) return;
  seen.add(p);
  const e = pkgs[p];
  if (!e) return;
  for (const d of Object.keys(e.dependencies || {})) walk(resolve(p, d));
  for (const d of Object.keys(e.peerDependencies || {})) walk(resolve(p, d));
}
for (const d of Object.keys(pj.dependencies)) walk(resolve("", d));
function license(p, e) {
  if (e.license) return e.license;
  try { return JSON.parse(fs.readFileSync(path.join(p, "package.json"))).license; } catch { return null; }
}
const direct = new Set(Object.keys(pj.dependencies));
const prod = [...seen].map((p) => {
  const e = pkgs[p], n = p.replace(/^.*node_modules\//, "");
  return { name: n, version: e.version, license: license(p, e), direct: direct.has(n) };
}).sort((a, b) => a.name.localeCompare(b.name));
const dev = Object.keys(pj.devDependencies).map((n) => {
  const p = "node_modules/" + n, e = pkgs[p] || {};
  return { name: n, version: e.version || pj.devDependencies[n], license: license(p, e) };
});
console.log(JSON.stringify({ prod, dev }));
"""


def frontend_tables() -> tuple[list[str], list[str]]:
    raw = subprocess.run(
        ["node", "-e", _NODE_SCRIPT],
        cwd=ROOT / "frontend",
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    data = json.loads(raw)
    prod = [
        f"| `{r['name']}` | {r['version']} | {r['license'] or 'see package metadata'} |"
        f" {'direct' if r['direct'] else 'transitive'} |"
        for r in data["prod"]
    ]
    dev = [
        f"| `{r['name']}` | {r['version']} | {r['license'] or 'see package metadata'} |"
        for r in data["dev"]
    ]
    return prod, dev


def main() -> int:
    if not (ROOT / "frontend" / "node_modules").is_dir():
        print(
            "frontend/node_modules is missing — run `npm ci` in frontend/ first "
            "so license fields can be read from installed packages.",
            file=sys.stderr,
        )
        return 1

    print("## Backend runtime dependencies\n")
    print("| Package | Version | License | Depth |")
    print("| --- | --- | --- | --- |")
    print("\n".join(backend_table()))

    prod, dev = frontend_tables()
    print("\n## Frontend runtime dependencies\n")
    print("| Package | Version | License | Depth |")
    print("| --- | --- | --- | --- |")
    print("\n".join(prod))

    print("\n## Frontend build-time dependencies\n")
    print("| Package | Version | License |")
    print("| --- | --- | --- |")
    print("\n".join(dev))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
