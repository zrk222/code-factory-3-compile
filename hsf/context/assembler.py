"""Layer assembly + doctrine hash.

Assembles the compile context in fixed order (Concepts -> Contracts -> Templates)
and computes the doctrine hash: sha256 over all library files + gate configs,
pinned into every LTAP receipt (Update stage syncs it; Audit verifies it).
"""
from __future__ import annotations
import hashlib
from pathlib import Path

LIB = Path(__file__).resolve().parent / "library"

def _read_sorted(globpat: str, base: Path) -> list[tuple[str, str]]:
    return [(p.relative_to(base).as_posix(), p.read_text()) for p in sorted(base.glob(globpat))]

def assemble_context(max_tokens: int = 32_000) -> dict:
    concepts = _read_sorted("concepts/*.md", LIB)
    contracts = _read_sorted("contracts/*.yaml", LIB)
    templates = _read_sorted("templates/*.j2", LIB)
    parts, pruned = [], []
    budget = max_tokens * 4  # ~4 chars/token heuristic
    for name, text in concepts + contracts + templates:
        if sum(len(t) for _, t in parts) + len(text) > budget and name.startswith("concepts/"):
            pruned.append(name)  # deterministic prune: concepts first
            continue
        parts.append((name, text))
    return {"parts": parts, "pruned": pruned}

def doctrine_hash(extra_paths: list[Path] | None = None) -> str:
    h = hashlib.sha256()
    files = sorted(LIB.rglob("*.*"))
    gates_dir = Path(__file__).resolve().parents[1] / "gates"
    files += sorted(gates_dir.glob("*.py"))
    for p in files + (extra_paths or []):
        h.update(str(p.name).encode())
        h.update(p.read_bytes())
    return h.hexdigest()
