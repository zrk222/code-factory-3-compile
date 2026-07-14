"""Machine-readable HSF installation and build provenance."""
from __future__ import annotations
import hashlib
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path
from . import __version__


def _commit(root: Path) -> str | None:
    for directory in (root, *root.parents):
        if (directory / ".git").exists():
            try:
                run = subprocess.run(["git", "rev-parse", "HEAD"], cwd=directory, capture_output=True, text=True, timeout=3)
            except OSError:
                return None
            return run.stdout.strip() if run.returncode == 0 else None
    return None


def _hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def provenance() -> dict:
    root = Path(__file__).resolve().parent
    direct_url = None
    origin = "source-tree"
    try:
        distribution = importlib.metadata.distribution("code-factory-3-compile")
        raw = distribution.read_text("direct_url.json")
        if raw:
            direct_url = json.loads(raw).get("url")
            origin = "direct-url"
        else:
            origin = "site-packages"
    except importlib.metadata.PackageNotFoundError:
        pass
    source_commit = _commit(root)
    build_hash = _hash(root)
    return {"schema": "factory.provenance.v1", "package": "code-factory-3-compile", "version": __version__,
            "source_commit": source_commit, "build_hash": build_hash, "install_origin": origin,
            "direct_url": direct_url, "python": sys.version.split()[0],
            "runtime": {"python": sys.version.split()[0], "implementation": sys.implementation.name},
            "receipt_schema": "factory.receipt.v2", "identity_complete": bool(source_commit and build_hash)}
