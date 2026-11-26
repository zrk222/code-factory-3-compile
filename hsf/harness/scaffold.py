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

TARGET_GATES: dict[str, list[dict[str, Any]]] = {
    "worker": [{"id": "lint", "kind": "lint", "run": "python -m ruff check ."},
               {"id": "unit", "kind": "unit", "run": "python -m pytest -q"}],
    "web": [{"id": "lint", "kind": "lint", "run": "npm run lint --if-present"},
            {"id": "unit", "kind": "unit", "run": "npm test --if-present"},
            {"id": "e2e", "kind": "e2e", "run": "npx playwright test", "timeout_s": 1800}],
    "mobile": [{"id": "lint", "kind": "lint", "run": "npm run lint --if-present"},
               {"id": "type", "kind": "type", "run": "npx tsc --noEmit"},
               {"id": "unit", "kind": "unit", "run": "npm test --if-present"}],
    "agent-ui": [{"id": "unit", "kind": "unit", "run": "npm test --if-present"},
                 {"id": "e2e", "kind": "e2e", "run": "npx playwright test", "timeout_s": 1800}],
    "generic": [{"id": "unit", "kind": "unit", "run": "python -m pytest -q"}],
}
TARGET_INIT = {"worker": ["python -m pip install -e ."], "web": ["npm ci"], "mobile": ["npm ci"],
               "agent-ui": ["npm ci"], "generic": []}
TARGET_MUTABLE = {"worker": ["src/**", "app/**"], "web": ["src/**", "app/**", "components/**", "pages/**"],
                  "mobile": ["src/**", "app/**", "components/**"], "agent-ui": ["src/**", "app/**"],
                  "generic": ["src/**"]}

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


def _spec_payload(name: str, target: str, with_cf: bool) -> dict[str, Any]:
    gates = [dict(gate) for gate in TARGET_GATES[target]] + (available_cf_gates() if with_cf else [])
    return {"harness": name, "version": 1, "target": target, "init": TARGET_INIT[target],
            "gates": gates, "mutable": TARGET_MUTABLE[target], "parallel_gates": min(4, len(gates)),
            "repair": {"command": None, "max_attempts": 3}}


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


def init_harness(name: str, root: Path = Path("."), target: str = "generic", with_cf: bool = True) -> list[Path]:
    root = Path(root)
    base = root / "harness"
    spec_path = base / "harness.yaml"
    if spec_path.exists():
        raise FileExistsError(f"{spec_path} already exists")
    if target not in TARGET_GATES:
        raise ValueError(f"unknown target {target!r}; choose one of {sorted(TARGET_GATES)}")
    base.mkdir(parents=True, exist_ok=True)
    payload = _spec_payload(name, target, with_cf)
    spec = HarnessSpec.model_validate(payload)
    spec_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    first = Feature(id="F001", category="foundation", description="The app starts and its gates pass.",
                    steps=["Run harness/init.sh", "Run hsf harness check"], gates=[g.id for g in spec.gates],
                    priority=1)
    Ledger(root / spec.features_path).seal([first])
    (root / spec.progress_path).write_text("", encoding="utf-8")
    init_sh = base / "init.sh"
    init_sh.write_text("#!/usr/bin/env sh\nset -e\n" + "".join(f"{cmd}\n" for cmd in spec.init), encoding="utf-8")
    init_sh.chmod(0o755)
    (root / "AGENTS.md").write_text(AGENTS_MD.format(name=name, repair_hint=REPAIR_HINT), encoding="utf-8") if not (
        root / "AGENTS.md").exists() else None
    _ignore_work_dir(root)
    seal_protected(root, spec)
    return [spec_path, root / spec.features_path, init_sh, base / PROTECTED_LOCK]
