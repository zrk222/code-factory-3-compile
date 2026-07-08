"""Deterministic Orchestrator — responsibilities are EXACTLY four (PRD 6.6):
read steps, resolve references, capture outputs, propagate state.
Runs with no LLM credentials (extractor holds its own restricted key)."""
from __future__ import annotations
import asyncio, hashlib, importlib.util, json, time
from pathlib import Path
from .audit import AuditLog
from .sandwich import SafetySandwich
from .state import StateStore

class RunContext:
    def __init__(self, sandwich: SafetySandwich, audit: AuditLog, spec_id: str, oob_policy: str = "human_review"):
        self._sandwich = sandwich
        self._audit = audit
        self._spec_id = spec_id
        self._oob_policy = oob_policy

    async def sandwich(self, schema: dict, text: str):
        out = await self._sandwich.invoke(schema, text)
        self._audit.append(step="extraction", spec_id=self._spec_id,
                           ok=out is not None, injection_flags=self._sandwich.flags)
        return out

    async def out_of_bounds(self, message: str, trace: str = "", policy: str | None = None):
        policy = policy or self._oob_policy
        self._audit.append(step="out_of_bounds", spec_id=self._spec_id,
                           policy=policy, message=message, trace=trace)
        mod = self._artifact_ns
        if policy == "reject":
            return mod["AuthResult"]("DENIED", message)
        return mod["AuthResult"]("HUMAN_REVIEW", message)

class Orchestrator:
    def __init__(self, artifact_path: Path, extractor, audit_path: Path | None = None,
                 verify=None, oob_policy: str = "human_review"):
        self.artifact_path = Path(artifact_path)
        if verify is not None:
            verify(self.artifact_path)  # signature check before load (registry.verify)
        src = self.artifact_path.read_text()
        self.ns: dict = {}
        exec(compile(src, str(self.artifact_path), "exec"), self.ns)
        self.cls = next(v for k, v in self.ns.items() if k.endswith("Workflow") and isinstance(v, type))
        self.spec_id = getattr(self.cls, "SPEC_ID", "unknown")
        self.audit = AuditLog(audit_path or Path("audit") / f"{self.spec_id}.jsonl")
        self.extractor = extractor
        self.state = StateStore()
        self.oob_policy = oob_policy

    async def run_async(self, input_data: dict):
        t0 = time.perf_counter()
        ctx = RunContext(SafetySandwich(self.extractor), self.audit, self.spec_id, self.oob_policy)
        ctx._artifact_ns = self.ns
        self.state.set("input_data", input_data)                       # propagate
        result = await self.cls().run(ctx, self.state.get("input_data"))  # read+resolve
        out = {"status": result.status, "reason": result.reason}
        h = hashlib.sha256(json.dumps(out, sort_keys=True).encode()).hexdigest()
        self.state.set("result", out)                                  # capture
        self.audit.append(step="result", spec_id=self.spec_id, output=out,
                          output_sha=h, latency_ms=round((time.perf_counter()-t0)*1000, 3))
        return result

    def run(self, input_data: dict):
        return asyncio.run(self.run_async(input_data))
