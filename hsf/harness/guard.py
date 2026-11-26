"""Repair guard: digest the tree, enforce protected, mutable and evidence surfaces, snapshot and roll back."""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

# Directories not digested file by file. Each still gets a summary entry (count, bytes, newest mtime), so a
# repair that writes into one is seen, unless the directory is listed in the spec's `unobserved`.
SKIP_DIRS = frozenset({".git", ".hsf-harness", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
                       ".mypy_cache", "dist", "build", ".next", ".expo"})
# Owned by the harness or by git; never summarized.
UNSUMMARIZED = frozenset({".git", ".hsf-harness"})
HARNESS_WRITES = ("harness/progress.jsonl", "harness/receipts/**")
# Evidence written by Code Factory tools and gates: append-only. New files are allowed and kept, existing files
# may not change (a .jsonl may only grow, keeping its old bytes as a prefix), and rollback never deletes them.
EVIDENCE = (".factory/**", ".forge/**", ".specline/**", ".hsf/**", "receipts/**", "envelopes/**")
CACHE_FILE = ".hsf-harness/gate-cache.json"
# Files outside mutable/protected/evidence globs are copied into a repair snapshot only up to this budget; a
# change to an uncopied file is reported as `rollback_incomplete` instead of silently surviving.
SNAPSHOT_BUDGET = 64 * 1024 * 1024
SUMMARY_PREFIX = "dir:"
LINK_PREFIX = "link:"


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


def _under_protected(rel_dir: str, protected) -> bool:
    """True when files inside this directory could match a protected glob."""
    probe = f"{rel_dir}/__hsf_probe__"
    return matches(probe, protected) or any(pattern.startswith(rel_dir + "/") for pattern in protected)


def _summary(path: Path) -> str:
    count = size = newest = 0
    for current, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                info = os.lstat(os.path.join(current, name))
            except OSError:
                continue
            count += 1
            size += info.st_size
            newest = max(newest, info.st_mtime_ns)
    return f"{SUMMARY_PREFIX}{count}:{size}:{newest}"


def _listing(path: Path) -> dict[str, tuple[int, int]]:
    """Absolute path -> (size, mtime_ns) for every file under a summarized directory."""
    out: dict[str, tuple[int, int]] = {}
    for current, _dirs, files in os.walk(path):
        for name in files:
            full = Path(current) / name
            try:
                info = os.lstat(full)
            except OSError:
                continue
            out[str(full)] = (info.st_size, info.st_mtime_ns)
    return out


def _entry(path: Path) -> str:
    if path.is_symlink():
        return LINK_PREFIX + os.readlink(path)
    return sha256(path.read_bytes()).hexdigest()


def tree_digests(root: Path, protected=()) -> dict[str, str]:
    """Relative POSIX path -> digest. Symlinks are recorded as their target, never followed; skipped
    directories are summarized (key ends with '/'), except beneath a protected glob, where every file counts."""
    root = Path(root)
    digests: dict[str, str] = {}
    for current, dirs, files in os.walk(root):
        rel_current = Path(current).relative_to(root).as_posix()
        keep = []
        for name in sorted(dirs):
            rel = name if rel_current == "." else f"{rel_current}/{name}"
            full = Path(current) / name
            if full.is_symlink():
                digests[rel] = LINK_PREFIX + os.readlink(full)
            elif name in SKIP_DIRS and not _under_protected(rel, protected):
                if name not in UNSUMMARIZED:
                    digests[rel + "/"] = _summary(full)
            else:
                keep.append(name)
        dirs[:] = keep
        for name in files:
            path = Path(current) / name
            if path.is_file() or path.is_symlink():
                digests[path.relative_to(root).as_posix()] = _entry(path)
    cache = root / CACHE_FILE
    if cache.is_file():
        digests[CACHE_FILE] = sha256(cache.read_bytes()).hexdigest()
    return digests


def changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))


@dataclass
class GuardVerdict:
    ok: bool
    changed: list[str] = field(default_factory=list)
    protected_touched: list[str] = field(default_factory=list)
    outside_mutable: list[str] = field(default_factory=list)
    evidence_rewritten: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {"ok": self.ok, "changed": self.changed, "protected_touched": self.protected_touched,
                "outside_mutable": self.outside_mutable, "evidence_rewritten": self.evidence_rewritten,
                "links": self.links}


def _evidence_ok(rel: str, before: dict[str, str], after: dict[str, str], root: Path, store: Path | None) -> bool:
    if rel not in before:
        return True
    if rel not in after or not rel.endswith(".jsonl") or store is None:
        return False
    old = (store / rel).read_bytes()
    return (Path(root) / rel).read_bytes().startswith(old)


def check(before: dict[str, str], after: dict[str, str], *, protected, mutable, unobserved=(),
          root: Path | None = None, store: Path | None = None) -> GuardVerdict:
    """A repair passes only if protected files, the gate cache and existing evidence are unchanged, no symlink
    was added or changed, and every other change is a regular file inside a mutable path."""
    changed = [p for p in changed_paths(before, after) if not matches(p, HARNESS_WRITES)]
    verdict = GuardVerdict(True, changed)
    for rel in changed:
        if rel.endswith("/"):
            if rel.rstrip("/").rsplit("/", 1)[-1] not in unobserved:
                verdict.outside_mutable.append(rel)
        elif rel == CACHE_FILE or matches(rel, protected):
            verdict.protected_touched.append(rel)
        elif after.get(rel, "").startswith(LINK_PREFIX) or before.get(rel, "").startswith(LINK_PREFIX):
            verdict.links.append(rel)
        elif matches(rel, EVIDENCE):
            if not _evidence_ok(rel, before, after, root or Path("."), store):
                verdict.evidence_rewritten.append(rel)
        elif not matches(rel, mutable):
            verdict.outside_mutable.append(rel)
    verdict.ok = not (verdict.protected_touched or verdict.outside_mutable or verdict.evidence_rewritten
                      or verdict.links)
    return verdict


class Snapshot:
    """Byte copies of the digested files (symlinks recorded, not followed) so a rejected repair is restored."""

    def __init__(self, root: Path, store: Path, digests: dict[str, str], keep_globs=(),
                 budget_bytes: int = SNAPSHOT_BUDGET):
        self.root, self.store, self.digests = Path(root), Path(store), dict(digests)
        if self.store.exists():
            shutil.rmtree(self.store)
        self.store.mkdir(parents=True)
        self.listings: dict[str, dict[str, tuple[int, int]]] = {}
        self.incomplete: list[str] = []
        self.uncopied: set[str] = set()
        spent = 0
        for rel, digest in sorted(self.digests.items(), key=lambda item: not matches(item[0], keep_globs)):
            if rel.endswith("/"):
                self.listings[rel] = _listing(self.root / rel)
                continue
            if digest.startswith(LINK_PREFIX):
                continue
            size = (self.root / rel).stat().st_size
            if keep_globs and not matches(rel, keep_globs) and spent + size > budget_bytes:
                self.uncopied.add(rel)
                continue
            spent += size
            target = self.store / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.root / rel, target)

    def _restore_one(self, rel: str) -> None:
        live = self.root / rel
        if live.is_symlink() or (live.exists() and not live.is_dir()):
            live.unlink()
        elif live.is_dir() and rel not in self.digests:
            shutil.rmtree(live)
        old = self.digests.get(rel)
        if old is None:
            return
        live.parent.mkdir(parents=True, exist_ok=True)
        if old.startswith(LINK_PREFIX):
            os.symlink(old[len(LINK_PREFIX):], live)
        else:
            shutil.copy2(self.store / rel, live)

    def restore(self, current: dict[str, str]) -> list[str]:
        """Restore changed files and links; new evidence files are kept, summarized directories are reported."""
        restored = []
        for rel in changed_paths(self.digests, current):
            if rel.endswith("/"):
                restored += self._restore_dir(rel)
                continue
            if matches(rel, HARNESS_WRITES):
                continue
            if rel not in self.digests and matches(rel, EVIDENCE):
                continue
            if rel in self.uncopied:
                self.incomplete.append(rel)
                continue
            self._restore_one(rel)
            restored.append(rel)
        return restored

    def _restore_dir(self, rel: str) -> list[str]:
        """Delete files a repair added inside a summarized directory; report ones it modified."""
        before = self.listings.get(rel, {})
        removed = []
        for path, stat in _listing(self.root / rel).items():
            if path not in before:
                Path(path).unlink()
                removed.append(path)
            elif before[path] != stat:
                self.incomplete.append(path)
        return removed

    def discard(self) -> None:
        shutil.rmtree(self.store, ignore_errors=True)
