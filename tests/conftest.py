import json, sys
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

@pytest.fixture(scope="session")
def spec_and_sha():
    from hsf.spec import load_spec
    return load_spec(ROOT / "specs" / "glp1_review.yaml")

@pytest.fixture(scope="session")
def goldens():
    p = ROOT / "goldens" / "glp1_review" / "cases.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]

@pytest.fixture(scope="session")
def spec_schema(spec_and_sha):
    spec, _ = spec_and_sha
    out = {}
    for s in spec.steps:
        if s.type == "bounded_invocation" and s.schema_:
            for k, fs in s.schema_.items():
                d = {"type": fs.type}
                if fs.min is not None: d["min"] = fs.min
                if fs.max is not None: d["max"] = fs.max
                out[k] = d
    return out

@pytest.fixture(scope="session")
def artifact_source(spec_and_sha):
    from hsf.foundry.compiler import compile_spec
    spec, sha = spec_and_sha
    src, meta = compile_spec(spec, sha, engine="template")
    return src
