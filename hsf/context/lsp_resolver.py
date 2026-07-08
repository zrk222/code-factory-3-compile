"""Symbol resolution over the Module Library (closed world).

v1 resolves against the REGISTRY manifest directly (exact, <1ms) —
functionally what LSP go-to-definition provides, without an editor server.
An unresolvable symbol blocks compilation (E_UNKNOWN_ACTIVITY): the
compiler can never hallucinate an API signature that isn't real.
"""
from __future__ import annotations
import inspect

class UnknownActivity(KeyError):
    pass

def resolve_signatures(activity_names: list[str]) -> dict[str, str]:
    from hsf.runtime.module_library import REGISTRY
    out = {}
    for name in activity_names:
        if name not in REGISTRY:
            raise UnknownActivity(f"E_UNKNOWN_ACTIVITY: {name!r} not in Module Library")
        out[name] = f"{name}{inspect.signature(REGISTRY[name])}"
    return out
