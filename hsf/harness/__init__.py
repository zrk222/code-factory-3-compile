"""Self-healing harness scaffolding: a checked feature ledger, verification gates and a bounded repair loop.

HSF never calls a model here. Repairs come from an external command (an agent CLI or a script) and are only
kept when they pass the guard (protected files unchanged, edits inside declared mutable paths) and improve
the gate results. Every attempt is receipted and rolled back when it is not kept.
"""
from .spec import HarnessSpec, GateSpec, RepairPolicy, load_harness
from .ledger import Feature, Ledger

__all__ = ["HarnessSpec", "GateSpec", "RepairPolicy", "load_harness", "Feature", "Ledger"]
