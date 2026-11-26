"""Repair guard: digest the tree, enforce protected and mutable surfaces, snapshot and roll back."""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

SKIP_DIRS = frozenset({".git", ".hsf-harness", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
                       ".mypy_cache", "dist", "build", ".next", ".expo"})
HARNESS_WRITES = ("harness/progress.jsonl", "harness/receipts/**")


@lru_cache(maxsize=256)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?"); i += 3
        elif pattern.startswith("**", i):
            out.append(".*"); i += 2
        elif pattern[i] == "*":
            out.append("[^/]*"); i += 1
        elif pattern[i] == "?":
            out.append("[^/]"); i += 1
        else:
            out.append(re.escape(pattern[i])); i += 1
    return re.compile("".join(out) + r"\Z")


def matches(path: str, patterns) -> bool:
    return any(_glob_regex(pattern).match(path) for pattern in patterns)


def tree_digests(root: Path) -> dict[str, str]:
    """Relative POSIX path -> SHA-256 for every regular file outside skipped directories."""
    root = Path(root)
    digests: dict[str, str] = {}
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in files:
            path = Path(current) / name
            if path.is_file() and not path.is_symlink():
                digests[path.relative_to(root).as_posix()] = sha256(path.read_bytes()).hexdigest()
    return digests


def changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))


@dataclass
class GuardVerdict:
    ok: bool
    changed: list[str] = field(default_factory=list)
    protected_touched: list[str] = field(default_factory=list)
    outside_mutable: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {"ok": self.ok, "changed": self.changed, "protected_touched": self.protected_touched,
                "outside_mutable": self.outside_mutable}


def check(before: dict[str, str], after: dict[str, str], *, protected, mutable) -> GuardVerdict:
    changed = [p for p in changed_paths(before, after) if not matches(p, HARNESS_WRITES)]
    touched = [p for p in changed if matches(p, protected)]
    outside = [p for p in changed if p not in touched and not matches(p, mutable)]
    return GuardVerdict(not touched and not outside, changed, touched, outside)


class Snapshot:
    """Byte copies of the files a repair may change, so a rejected attempt is restored exactly."""

    def __init__(self, root: Path, store: Path, digests: dict[str, str]):
        self.root, self.store, self.digests = Path(root), Path(store), dict(digests)
        if self.store.exists():
            shutil.rmtree(self.store)
        self.store.mkdir(parents=True)
        for rel in self.digests:
            target = self.store / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.root / rel, target)

    def restore(self, current: dict[str, str]) -> list[str]:
        restored = []
        for rel in changed_paths(self.digests, current):
            live = self.root / rel
            if rel in self.digests:
                live.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.store / rel, live)
            elif live.exists():
                live.unlink()
            restored.append(rel)
        return restored

    def discard(self) -> None:
        shutil.rmtree(self.store, ignore_errors=True)
