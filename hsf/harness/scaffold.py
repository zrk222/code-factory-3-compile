"""`hsf harness init`: scaffold an app harness (spec, sealed feature ledger, progress log, init script, agent rules)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from .cf import REPAIR_HINT, available_cf_gates
from .guard import tree_digests, matches
from .ledger import Feature, Ledger
from .spec import HarnessSpec

PROTECTED_LOCK = "protected.lock.json"

_NODE_LINT = {"id": "lint", "kind": "lint", "run": "npm run lint", "npm_script": "lint"}
_NODE_TEST = {"id": "unit", "kind": "unit", "run": "npm test", "npm_script": "test"}
_PLAYWRIGHT = {"id": "e2e", "kind": "e2e", "run": "npx playwright test", "timeout_s": 1800}
_PY_LINT = {"id": "lint", "kind": "lint", "run": "python -m ruff check ."}
_PY_TEST = {"id": "unit", "kind": "unit", "run": "python -m pytest -q"}
# Gates never use `--if-present`: a missing script must fail, not pass with nothing to run (HH-7).
TARGET_GATES: dict[str, list[dict[str, Any]]] = {
    "worker": [_PY_LINT, _PY_TEST],
    "web": [_NODE_LINT, _NODE_TEST, _PLAYWRIGHT],
    "mobile": [_NODE_LINT, {"id": "type", "kind": "type", "run": "npx tsc --noEmit"}, _NODE_TEST],
    "agent-ui": [_NODE_TEST, _PLAYWRIGHT],
    "api": [_PY_LINT, _PY_TEST],
    "cli": [_PY_LINT, _PY_TEST],
    "mcp": [_PY_LINT, _PY_TEST],
    "generic": [_PY_TEST],
}
TARGET_INIT = {"worker": ["python -m pip install -e ."], "web": ["npm ci"], "mobile": ["npm ci"],
               "agent-ui": ["npm ci"], "api": ["python -m pip install -e ."], "cli": ["python -m pip install -e ."],
               "mcp": ["python -m pip install -e ."], "generic": []}
TARGET_MUTABLE = {"worker": ["src/**", "app/**"], "web": ["src/**", "app/**", "components/**", "pages/**"],
                  "mobile": ["src/**", "app/**", "components/**"], "agent-ui": ["src/**", "app/**"],
                  "api": ["src/**", "app/**"], "cli": ["src/**"], "mcp": ["src/**"], "generic": ["src/**"]}
AGENTS_START, AGENTS_END = "<!-- hsf-harness:start -->", "<!-- hsf-harness:end -->"


AGENTS_MD = """# Harness rules for {name}

This harness was scaffolded by HSF. Every coding session follows the same routine:

1. `hsf harness status` shows the working directory, ledger integrity, protected-file integrity,
   the next unfinished feature and the latest progress entries.
2. Run `harness/init.sh` if the environment is not ready.
3. `hsf harness check` must pass before new work starts. If it fails, fix that first
   (`hsf harness heal` runs the bounded repair loop).
4. Work on exactly one feature: the one `status` names.
5. `hsf harness pass <feature-id>` marks it done only after its own gates pass end to end.
6. Commit with a descriptive message. The progress log is updated by the harness.

Rules:
- Edit only files that match `mutable` in `harness/harness.yaml`.
- Never edit, delete or weaken tests, goldens, specs, the ledger text or the gate commands.
  These are protected and digest-locked; `status` and `heal` reject any change to them.
- In `harness/features.json` only `passes` may change, and only through `hsf harness pass`.
  New features go through `hsf harness add-feature`. Owner edits to protected files are re-sealed
  explicitly with `hsf harness seal --reason "..."`, which records the files and reason in the progress log.

Repair: set `repair.command` in `harness/harness.yaml` to the agent that fixes failures. It receives the
failure report path in `HSF_FAILURE_REPORT`. With Code Factory installed, run the agent as an observed,
validated evidence session:
`{repair_hint}`
"""


def _missing_npm_scripts(root: Path, gates: list[dict[str, Any]]) -> list[str]:
    package = Path(root) / "package.json"
    if not package.is_file():
        return []
    scripts = (json.loads(package.read_text(encoding="utf-8")) or {}).get("scripts") or {}
    return [gate["npm_script"] for gate in gates if gate.get("npm_script") and gate["npm_script"] not in scripts]


def _spec_payload(name: str, target: str, cf_mode: str, root: Path) -> dict[str, Any]:
    gates = [dict(gate) for gate in TARGET_GATES[target]]
    missing = _missing_npm_scripts(root, gates)
    if missing:
        raise ValueError(f"package.json has no {', '.join(repr(m) for m in missing)} script; add it or edit the "
                         "gates in harness/harness.yaml after init (gates never pass when a script is missing)")
    for gate in gates:
        gate.pop("npm_script", None)
    gates += available_cf_gates(mode=cf_mode)
    return {"harness": name, "version": 1, "target": target, "code_factory": cf_mode, "init": TARGET_INIT[target],
            "gates": gates, "mutable": TARGET_MUTABLE[target], "parallel_gates": min(4, len(gates)),
            "repair": {"command": None, "max_attempts": 3}}


def write_agent_rules(root: Path, name: str) -> Path:
    """Write the harness rules as a delimited block, appended to (or refreshed in) an existing AGENTS.md (HH-12)."""
    path = Path(root) / "AGENTS.md"
    block = f"{AGENTS_START}\n{AGENTS_MD.format(name=name, repair_hint=REPAIR_HINT).rstrip()}\n{AGENTS_END}\n"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if AGENTS_START in text and AGENTS_END in text:
        head, rest = text.split(AGENTS_START, 1)
        text = head + block + rest.split(AGENTS_END, 1)[1].lstrip("\n")
    else:
        text = text + ("\n" if text and not text.endswith("\n") else "") + ("\n" if text else "") + block
    path.write_text(text, encoding="utf-8")
    return path


def _protected_now(root: Path, spec: HarnessSpec) -> dict[str, str]:
    return {path: digest for path, digest in tree_digests(root, spec.protected).items()
            if matches(path, spec.protected) and not path.endswith(PROTECTED_LOCK)}


def _lock_path(root: Path) -> Path:
    return Path(root) / "harness" / PROTECTED_LOCK


def ledger_paths(spec: HarnessSpec) -> list[str]:
    """The two protected files the harness itself rewrites: the ledger and its seal."""
    return [spec.features_path, str(Path(spec.features_path).parent / "ledger.lock.json").replace("\\", "/")]


def seal_protected(root: Path, spec: HarnessSpec, only=None) -> dict[str, str]:
    """Record digests of protected files. With `only`, update just those paths and keep every other seal."""
    current = _protected_now(root, spec)
    lock = _lock_path(root)
    if only is None or not lock.exists():
        sealed = current
    else:
        sealed = json.loads(lock.read_text(encoding="utf-8"))
        for path in only:
            if path in current:
                sealed[path] = current[path]
            else:
                sealed.pop(path, None)
    lock.write_text(json.dumps(sealed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return sealed


def protected_drift(root: Path, spec: HarnessSpec, ignore=()) -> list[str]:
    """Protected files whose bytes differ from the sealed lock (added, changed or removed)."""
    sealed = json.loads(_lock_path(root).read_text(encoding="utf-8"))
    current = _protected_now(root, spec)
    return sorted(path for path in set(sealed) | set(current)
                  if sealed.get(path) != current.get(path) and path not in ignore)


def _ignore_work_dir(root: Path) -> None:
    gitignore = Path(root) / ".gitignore"
    text = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if ".hsf-harness/" not in text.splitlines():
        gitignore.write_text(text + ("" if not text or text.endswith("\n") else "\n") + ".hsf-harness/\n",
                             encoding="utf-8")


def init_harness(name: str, root: Path = Path("."), target: str = "generic", with_cf: bool | None = None,
                 cf_mode: str | None = None) -> list[Path]:
    root = Path(root)
    base = root / "harness"
    spec_path = base / "harness.yaml"
    if spec_path.exists():
        raise FileExistsError(f"{spec_path} already exists")
    if target not in TARGET_GATES:
        raise ValueError(f"unknown target {target!r}; choose one of {sorted(TARGET_GATES)}")
    mode = cf_mode or ("auto" if with_cf in (None, True) else "off")
    payload = _spec_payload(name, target, mode, root)
    base.mkdir(parents=True, exist_ok=True)
    spec = HarnessSpec.model_validate(payload)
    spec_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    first = Feature(id="F001", category="foundation", description="The app starts and its gates pass.",
                    steps=["Run harness/init.sh", "Run hsf harness check"],
                    gates=[g.id for g in spec.gates if g.required], priority=1)
    Ledger(root / spec.features_path).seal([first])
    (root / spec.progress_path).write_text("", encoding="utf-8")
    init_sh = base / "init.sh"
    init_sh.write_text("#!/usr/bin/env sh\nset -e\n" + "".join(f"{cmd}\n" for cmd in spec.init), encoding="utf-8")
    init_sh.chmod(0o755)
    write_agent_rules(root, name)
    _ignore_work_dir(root)
    seal_protected(root, spec)
    return [spec_path, root / spec.features_path, init_sh, base / PROTECTED_LOCK]
