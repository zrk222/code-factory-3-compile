"""hsf serve — any signed artifact becomes a REST endpoint (FastAPI, optional extra).

POST /run {"text": "...", "extracted": {...}} -> {"status", "reason"}
GET  /healthz -> artifact identity
Extraction: fixture fields (offline) or the quarantined AnthropicExtractor
if HSF_EXTRACTOR_KEY is set. The serving layer holds no generation-plane creds.
"""
import os
from pathlib import Path

def build_app(artifact_path: str):
    try:
        from fastapi import Body, FastAPI
        from pydantic import BaseModel
    except ImportError as e:
        raise RuntimeError("pip install 'harness-factory[serve]' for hsf serve") from e
    from hsf.registry import verify_artifact
    from hsf.runtime import Orchestrator
    from hsf.runtime.extractor import FixtureExtractor, AnthropicExtractor

    class RunReq(BaseModel):
        text: str = ""
        extracted: dict | None = None

    app = FastAPI(title="HSF Artifact Server")

    @app.get("/healthz")
    def health():
        orch = Orchestrator(Path(artifact_path), FixtureExtractor({}), verify=verify_artifact)
        return {"spec_id": orch.spec_id, "artifact": Path(artifact_path).name,
                "spec_sha": getattr(orch.cls, "SPEC_SHA", "")[:16]}

    @app.post("/run")
    async def run(req: RunReq = Body(...)):
        ex = (FixtureExtractor(req.extracted) if req.extracted is not None
              else AnthropicExtractor() if os.environ.get("HSF_EXTRACTOR_KEY")
              else FixtureExtractor({}))
        orch = Orchestrator(Path(artifact_path), ex, verify=verify_artifact)
        r = await orch.run_async({"text": req.text})
        return {"status": r.status, "reason": r.reason}
    return app
