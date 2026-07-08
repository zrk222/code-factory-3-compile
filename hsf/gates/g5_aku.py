"""AKU validator triad gate.

This gate makes the governance gradient executable: pre, post, and invariant
validators produce findings and evidence rather than remaining descriptive text.
"""
from __future__ import annotations

from hsf.gates.base import Finding, GateResult
from hsf.spec.models import SpecModel


def _receipt_gate_names(receipt: dict | None) -> set[str]:
    return {g.get("gate") for g in (receipt or {}).get("gates", []) if isinstance(g, dict)}


def run(spec: SpecModel, receipt: dict | None = None, *, require_autonomous: bool = False) -> GateResult:
    findings: list[Finding] = []
    evidence = {
        "pre": {},
        "post": {},
        "invariant": {},
        "require_autonomous": require_autonomous,
    }

    evidence["pre"]["has_intent"] = bool(spec.workflow_spec and spec.version)
    evidence["pre"]["has_io_contract"] = bool(spec.inputs and spec.outputs)
    evidence["pre"]["has_steps"] = bool(spec.steps)
    if not evidence["pre"]["has_intent"]:
        findings.append(Finding("AKU_PRE_INTENT", "high", "workflow intent is missing"))
    if not evidence["pre"]["has_io_contract"]:
        findings.append(Finding("AKU_PRE_IO", "high", "inputs/outputs contract is missing"))
    if not evidence["pre"]["has_steps"]:
        findings.append(Finding("AKU_PRE_STEPS", "high", "procedure steps are missing"))

    gate_names = _receipt_gate_names(receipt)
    expected_gates = {"security", "syntax", "execution", "accuracy"}
    passed_gates = {
        g.get("gate")
        for g in (receipt or {}).get("gates", [])
        if isinstance(g, dict) and g.get("passed") is True
    }
    evidence["post"]["receipt_present"] = receipt is not None
    evidence["post"]["required_gates_present"] = sorted(expected_gates & gate_names)
    evidence["post"]["all_required_gates_passed"] = expected_gates <= passed_gates
    evidence["post"]["shipped_receipt"] = bool(receipt and receipt.get("shipped") is True)
    if receipt and not evidence["post"]["all_required_gates_passed"]:
        missing = sorted(expected_gates - passed_gates)
        findings.append(Finding("AKU_POST_GATES", "high", f"required receipt gates not passed: {missing}"))
    if require_autonomous and not evidence["post"]["shipped_receipt"]:
        findings.append(Finding("AKU_POST_SHIPPED", "high", "autonomy requires a shipped receipt"))

    branch_steps = [s for s in spec.steps if s.type == "branch"]
    bounded_steps = [s for s in spec.steps if s.type == "bounded_invocation"]
    evidence["invariant"]["deterministic_branch_logic"] = bool(branch_steps)
    evidence["invariant"]["bounded_inputs"] = bool(bounded_steps)
    evidence["invariant"]["runtime_model_calls"] = 0
    evidence["invariant"]["prompt_injection_surface"] = "generation-plane only"
    if not branch_steps:
        findings.append(Finding("AKU_INV_BRANCH", "medium", "no deterministic branch logic found"))

    autonomy_ready = (
        evidence["pre"]["has_intent"]
        and evidence["pre"]["has_io_contract"]
        and evidence["pre"]["has_steps"]
        and evidence["post"]["shipped_receipt"]
        and evidence["post"]["all_required_gates_passed"]
        and evidence["invariant"]["deterministic_branch_logic"]
    )
    evidence["autonomy_ready"] = autonomy_ready

    return GateResult("aku_validators", not findings, findings, evidence)
