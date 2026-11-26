"""Optional Code Factory integration. Every call degrades safely when the tools are absent:
gate planning falls back to running every gate, and `heal-verify` fails closed (the repair is rolled back).

- CF gates for `init` (`forge qa`, `factory scan`)
- `factory journey heal-verify` for kept repairs (3.2 c)
- `factory proofs plan` / `record` as the gate cache (3.2 d)
- seeding the ledger from SpecLine plans and ForgeLine smoke manifests (3.2 e)
"""
from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from .ledger import Feature
from .spec import GateSpec, HarnessSpec

PROOF_MANIFEST = ".factory/proof-requests/harness.json"
ACCEPTED_HEAL_DECISIONS = frozenset({"passed", "admissible_for_human_review"})
REPAIR_HINT = ("factory wrap --root . --admission <READY admission packet> --validators <validators.json> "
               "--run-id heal-<unique-id> -- <agent-cli>")


def available_cf_gates(which=None, mode: str = "auto", feature: str | None = None,
                       ssat: str | None = None) -> list[dict]:
    """CF gates. `auto` adds what is installed, `on` adds both regardless, `off` adds none (HH-12). Repo-wide
    `forge qa` is advisory (ForgeLine treats it as non-gating); with an SSAT it is feature-scoped and blocking."""
    if mode == "off":
        return []
    lookup = which or shutil.which
    present = (lambda tool: True) if mode == "on" else (lambda tool: bool(lookup(tool)))
    gates = []
    if present("forge"):
        if feature and ssat:
            gates.append({"id": "cf-quality", "kind": "lint", "required": True, "timeout_s": 1800,
                          "run": f"forge qa {feature} --ssat {ssat} --root . --strict"})
        else:
            gates.append({"id": "cf-quality", "kind": "lint", "required": False, "timeout_s": 1800,
                          "run": "forge qa --repo-wide --root . --strict"})
    if present("factory"):
        gates.append({"id": "cf-scan", "kind": "custom", "required": False, "timeout_s": 1800,
                      "run": "factory scan --static-only --root . --json"})
    return gates


def _factory(args: list[str], root: Path, timeout_s: int, run=subprocess.run, which=None) -> dict | None:
    exe = (which or shutil.which)("factory")
    if not exe:
        return None
    try:
        done = run([exe, *args, "--root", str(root), "--json"], cwd=root, capture_output=True, text=True,
                   timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        return json.loads(done.stdout) | {"_exit_code": done.returncode}
    except (ValueError, TypeError):
        return {"_exit_code": done.returncode, "_stderr": (done.stderr or "")[-400:]}


# --- (c) heal-verify -------------------------------------------------------------------------------------------

def allowed_paths(spec: HarnessSpec) -> list[str]:
    """Directory prefixes of the mutable globs (heal-verify checks containment by path prefix)."""
    out = []
    for pattern in spec.mutable:
        prefix = re.split(r"[*?\[]", pattern, maxsplit=1)[0].rstrip("/")
        out.append(prefix or ".")
    return sorted(set(out))


def healing_input(root: Path, spec: HarnessSpec, heal_id: str, patch: tuple[Path, str], changed: list[str],
                  coverage_before: list[str], coverage_after: list[str], failed_gates: list[str],
                  snapshot_dir: Path) -> Path:
    """Write a factory.proof-gated-healing-input.v1 document for one kept repair."""
    gates = [arg for gate in failed_gates for arg in ("--gate", gate)]
    anchor = {"role": "harness", "label": spec.harness, "route": ",".join(failed_gates) or "all", "state": "required"}
    data = {
        "schema": "factory.proof-gated-healing-input.v1", "healing_id": heal_id, "review_mode": "human_controlled",
        "agent": None,
        "patch": {"path": patch[0].relative_to(root).as_posix(), "sha256": patch[1], "changed_paths": changed},
        "allowed_paths": allowed_paths(spec),
        "semantic_identity": {"before": anchor, "after": anchor},
        "coverage_before": coverage_before, "coverage_after": coverage_after,
        "positive_argv": ["hsf", "harness", "check", "--no-cache", "--root", ".", *gates],
        "negative_argv": ["hsf", "harness", "check", "--no-cache", "--allow-drift", "--root", str(snapshot_dir), *gates],
    }
    out = Path(root) / ".hsf-harness" / f"healing-{heal_id}.json"
    out.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return out


def heal_verify(root: Path, input_path: Path, *, timeout_s: int = 300, run=subprocess.run,
                which=None) -> dict | None:
    """`factory journey heal-verify`; None when factory is not installed or did not answer."""
    return _factory(["journey", "heal-verify", str(input_path), "--timeout-seconds", str(timeout_s)],
                    Path(root), timeout_s + 30, run, which)


def heal_verify_accepts(result: dict | None) -> bool:
    return result is not None and result.get("decision") in ACCEPTED_HEAL_DECISIONS


# --- (d) proof reuse -------------------------------------------------------------------------------------------

def proof_manifest(root: Path, spec: HarnessSpec, digests: dict[str, str]) -> dict:
    from hsf import __version__
    from .guard import matches
    gates = []
    for gate in spec.gates:
        if not gate.inputs:
            continue
        inputs = sorted(path for path in digests if not path.endswith("/") and matches(path, gate.inputs))
        gates.append({"name": f"harness:{gate.id}", "command": ["sh", "-c", gate.run],
                      "inputs": inputs + ["harness/harness.yaml"],
                      "toolchain": {"hsf": __version__, "python": platform.python_version()},
                      "environment": {"PYTHONDONTWRITEBYTECODE": "1"}, "read_only": gate.read_only,
                      "safe_to_skip": gate.safe_to_skip, "relevant_paths": gate.relevant_paths})
    return {"schema": "factory.proof-request.v1", "gates": gates}


def plan_gates(root: Path, spec: HarnessSpec, digests: dict[str, str], changed: list[str] | None = None, *,
               run=subprocess.run, which=None) -> dict[str, str] | None:
    """Gate id -> RUN | REUSE | SKIP | BLOCK from `factory proofs plan`; None means run everything."""
    manifest = Path(root) / PROOF_MANIFEST
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(proof_manifest(root, spec, digests), indent=2, sort_keys=True), encoding="utf-8")
    args = ["proofs", "plan", PROOF_MANIFEST, *[arg for path in changed or [] for arg in ("--changed", path)]]
    result = _factory(args, Path(root), 300, run, which)
    if not result or result.get("_exit_code") not in (0, None):
        return None
    out = {}
    for item in result.get("items") or result.get("gates") or []:
        name = str(item.get("gate", ""))
        if name.startswith("harness:"):
            out[name.split(":", 1)[1]] = str(item.get("disposition", "RUN"))
    return out


def record_gate(root: Path, gate_id: str, elapsed_ms: int, *, run=subprocess.run, which=None) -> bool:
    result = _factory(["proofs", "record", PROOF_MANIFEST, "--gate", f"harness:{gate_id}", "--elapsed-ms",
                       str(max(0, elapsed_ms)), "--replace"], Path(root), 300, run, which)
    return result is not None and result.get("_exit_code") == 0


def planner_for(root: Path, spec: HarnessSpec, digests: dict[str, str]) -> Callable[[], dict | None] | None:
    if spec.cache != "factory-proofs":
        return None
    return lambda: plan_gates(root, spec, digests)


# --- (e) seeding -----------------------------------------------------------------------------------------------

_TASK = re.compile(r"^\s*-\s*\[[ xX]\]\s*(?P<id>T\d+)\s*\|(?P<fields>.*)$")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:20] or "f"


def _parse_tasks(text: str) -> list[dict[str, Any]]:
    try:
        from specline.plan_lint import parse_tasks  # type: ignore[import-not-found]
        return list(parse_tasks(text))
    except Exception:  # noqa: BLE001 - SpecLine absent or a different version: use the documented line format
        pass
    tasks = []
    for line in text.splitlines():
        match = _TASK.match(line)
        if not match:
            continue
        parts = [part.strip() for part in match.group("fields").split("|")]
        fields = dict(part.split("=", 1) for part in parts if "=" in part)
        text_part = next((part for part in reversed(parts) if "=" not in part), "")
        verify = fields.get("verify", "").strip("`")
        files = [item.strip() for item in fields.get("files", "").split(",") if item.strip()]
        tasks.append({"id": match.group("id"), "verify": verify, "files": files, "text": text_part})
    return tasks


def seed_from_specline(root: Path, feature: str) -> tuple[list[GateSpec], list[Feature]]:
    plan = Path(root) / "plans" / f"{feature}.md"
    if not plan.is_file():
        return [], []
    gates, features = [], []
    for task in _parse_tasks(plan.read_text(encoding="utf-8")):
        task_id = str(task.get("id", "")).strip()
        verify = str(task.get("verify", "")).strip()
        if not task_id or not verify:
            continue
        gate = GateSpec(id=f"sl-{_slug(feature)}-{task_id.lower()}", kind="unit", run=verify,
                        inputs=[*task.get("files", []), f"specs/{feature}.md", f"plans/{feature}.md"])
        gates.append(gate)
        features.append(Feature(id=f"{feature}.{task_id}", category="task",
                                description=str(task.get("text") or task_id), steps=[f"Run: {verify}"],
                                gates=[gate.id]))
    return gates, features


def seed_from_forgeline(root: Path, feature: str) -> tuple[list[GateSpec], list[Feature]]:
    smoke = Path(root) / "smoke" / f"{feature}.json"
    if not smoke.is_file():
        return [], []
    gate = GateSpec(id=f"forge-smoke-{_slug(feature)}", kind="e2e", run=f"forge smoke {feature} --root .",
                    inputs=[f"smoke/{feature}.json"])
    try:
        checks = json.loads(smoke.read_text(encoding="utf-8")).get("checks") or []
    except (ValueError, AttributeError):
        checks = []
    names = [str(check.get("name")) for check in checks if isinstance(check, dict) and check.get("name")]
    features = [Feature(id=f"{feature}.smoke.{_slug(name)}", category="behaviour", description=name,
                        steps=[f"forge smoke {feature}"], gates=[gate.id]) for name in names or ["smoke"]]
    return [gate], features


SEED_PROTECTED = ("plans/**", "smoke/**", "**/*.ssat.yaml")
