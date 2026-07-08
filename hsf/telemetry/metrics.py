"""Cost/latency telemetry: break-even math + runtime entropy check."""
from __future__ import annotations
import hashlib, json

def break_even(compile_tokens: int, interpretive_tokens_per_tx: int = 2000,
               runtime_tokens_per_tx: int = 0, price_per_1k: float = 0.01) -> dict:
    """n* = compile_tokens / (interpretive - runtime) per-tx token delta."""
    delta = interpretive_tokens_per_tx - runtime_tokens_per_tx
    n_star = compile_tokens / delta if delta > 0 else float("inf")
    def cost(n, per_tx, fixed=0):
        return (fixed + n * per_tx) * price_per_1k / 1000
    return {
        "n_star": round(n_star, 2),
        "tco_interpretive_1k": round(cost(1_000, interpretive_tokens_per_tx), 4),
        "tco_compiled_1k": round(cost(1_000, runtime_tokens_per_tx, compile_tokens), 4),
        "tco_interpretive_1m": round(cost(1_000_000, interpretive_tokens_per_tx), 2),
        "tco_compiled_1m": round(cost(1_000_000, runtime_tokens_per_tx, compile_tokens), 2),
    }

class DeterminismViolation(RuntimeError):
    pass

def entropy_check(outputs: list[dict]) -> str:
    """All identical-input replays must hash identically (H = 0)."""
    hashes = {hashlib.sha256(json.dumps(o, sort_keys=True).encode()).hexdigest() for o in outputs}
    if len(hashes) > 1:
        raise DeterminismViolation(f"entropy detected: {len(hashes)} distinct outputs")
    return next(iter(hashes)) if hashes else ""
