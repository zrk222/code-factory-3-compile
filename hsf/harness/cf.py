"""Optional Code Factory integration: reuse installed CF tools as harness gates, without a hard dependency."""
from __future__ import annotations

import shutil

CF_GATES = (
    ("forge", {"id": "cf-quality", "kind": "lint", "run": "forge qa --repo-wide --root . --strict",
               "required": True, "timeout_s": 1800}),
    ("factory", {"id": "cf-scan", "kind": "custom", "run": "factory scan --static-only --root . --json",
                 "required": False, "timeout_s": 1800}),
)
REPAIR_HINT = ("factory wrap --root . --admission harness/admission.json --validators harness/validators.json "
               "--run-id heal-$(date +%s) -- <agent-cli>")


def available_cf_gates(which=shutil.which) -> list[dict]:
    """CF gates whose CLI is installed: ForgeLine quality (blocking) and a Code Factory scan (advisory)."""
    return [dict(gate) for tool, gate in CF_GATES if which(tool)]
