"""Atomic Knowledge Unit export and topology validation."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import yaml

from hsf.spec.models import SpecModel

Autonomy = Literal["human_controlled", "supervised", "autonomous"]


@dataclass(frozen=True)
class ValidatorCoverage:
    pre: list[str]
    post: list[str]
    invariant: list[str]


@dataclass(frozen=True)
class AtomicKnowledgeUnit:
    intent: dict[str, object]
    procedure: list[str]
    tools: list[str]
    metadata: dict[str, object]
    governance: dict[str, object]
    continuations: dict[str, str]
    validators: ValidatorCoverage
    autonomy: Autonomy


def _branch_count(spec: SpecModel) -> int:
    return sum(1 for step in spec.steps if step.type == "branch")


def _bounded_count(spec: SpecModel) -> int:
    return sum(1 for step in spec.steps if step.type == "bounded_invocation")


def validator_coverage(spec: SpecModel, receipt: dict | None = None) -> ValidatorCoverage:
    pre = ["spec_loader", "bounded_schema", "branch_reference_check"]
    if spec.metadata.compliance:
        pre.append("registered_compliance_guards")
    if _bounded_count(spec):
        pre.append("out_of_bounds_policy")

    post = ["syntax_gate", "golden_accuracy_gate"]
    if receipt:
        post.append("receipt_integrity")
        if receipt.get("shipped") is True:
            post.append("shipped_artifact")

    invariant = ["prompt_injection_audit", "no_network_or_secret_forwarding"]
    if _branch_count(spec):
        invariant.append("deterministic_branch_logic")

    return ValidatorCoverage(pre=pre, post=post, invariant=invariant)


def classify_autonomy(coverage: ValidatorCoverage, receipt: dict | None = None) -> Autonomy:
    has_pre = bool(coverage.pre)
    has_post = bool(coverage.post)
    has_invariant = bool(coverage.invariant)
    shipped = bool(receipt and receipt.get("shipped") is True)
    if has_pre and has_post and has_invariant and shipped:
        return "autonomous"
    if has_pre and has_post:
        return "supervised"
    return "human_controlled"


def export_aku(spec: SpecModel, spec_sha: str, receipt_path: str | Path | None = None) -> AtomicKnowledgeUnit:
    receipt = None
    if receipt_path:
        receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))

    coverage = validator_coverage(spec, receipt)
    procedure = [
        f"Load workflow spec {spec.workflow_spec} v{spec.version}.",
        "Validate schema, step references, branch exhaustiveness, and compliance tags.",
        "Compile the decision workflow into static Python.",
        "Run security, syntax, execution, accuracy, and injection gates.",
        "Store the signed artifact and receipt only when gates pass.",
    ]
    if receipt:
        procedure.append("Use the receipt as the public evidence source.")

    return AtomicKnowledgeUnit(
        intent={
            "workflow_spec": spec.workflow_spec,
            "trigger": "Use when this recurring decision workflow must run deterministically.",
            "spec_sha256": spec_sha,
        },
        procedure=procedure,
        tools=["hsf validate", "hsf compile", "hsf goldens", "hsf run", "hsf badge"],
        metadata={
            "owner": spec.metadata.owner,
            "version": spec.version,
            "compliance": list(spec.metadata.compliance),
            "inputs": sorted(spec.inputs.keys()),
            "outputs": sorted(spec.outputs.keys()),
        },
        governance={
            "runtime_model_calls": 0,
            "prompt_injection_surface": "generation-plane only; runtime decision logic is static code",
            "out_of_bounds_policies": sorted(
                {step.on_out_of_bounds for step in spec.steps if step.on_out_of_bounds}
            ),
            "autonomy_requires": "pre, post, and invariant validators plus shipped receipt history",
        },
        continuations={
            "success": "publish receipt, badge, and signed artifact",
            "failure": "repair spec or compiler and rerun all gates",
            "escalation": "human review for ambiguous or out-of-policy inputs",
        },
        validators=coverage,
        autonomy=classify_autonomy(coverage, receipt),
    )


def write_aku(aku: AtomicKnowledgeUnit, output: str | Path) -> Path:
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(asdict(aku), indent=2, sort_keys=True), encoding="utf-8")
    return out


def validate_topology(path: str | Path) -> dict[str, object]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError("E_TOPOLOGY_SHAPE: nodes and edges must be lists")

    node_ids = {n["id"] if isinstance(n, dict) else n for n in nodes}
    if len(node_ids) != len(nodes):
        raise ValueError("E_TOPOLOGY_DUPLICATE: duplicate node id")

    graph = {node: [] for node in node_ids}
    for edge in edges:
        src = edge.get("from")
        dst = edge.get("to")
        if src not in node_ids or dst not in node_ids:
            raise ValueError(f"E_TOPOLOGY_DANGLING: {src!r} -> {dst!r}")
        graph[src].append(dst)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ValueError(f"E_TOPOLOGY_CYCLE: {node}")
        if node in visited:
            return
        visiting.add(node)
        for nxt in graph[node]:
            visit(nxt)
        visiting.remove(node)
        visited.add(node)

    for node in sorted(node_ids):
        visit(node)

    return {"nodes": len(node_ids), "edges": len(edges), "valid": True}
