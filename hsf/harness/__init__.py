"""Self-healing harness scaffolding: a checked feature ledger, verification gates and a bounded repair loop.

HSF never calls a model here. Repairs come from an external command (an agent CLI or a script) and are only
kept when they pass the guard (protected files unchanged, edits inside declared mutable paths) and improve
the gate results. Every attempt is receipted and rolled back when it is not kept.
"""
from importlib import import_module

_EXPORTS = {"HarnessSpec": "spec", "GateSpec": "spec", "RepairPolicy": "spec", "load_harness": "spec",
            "Feature": "ledger", "Ledger": "ledger"}
__all__ = list(_EXPORTS)


def __getattr__(name: str):
    if name in _EXPORTS:
        return getattr(import_module(f".{_EXPORTS[name]}", __name__), name)
    raise AttributeError(name)
