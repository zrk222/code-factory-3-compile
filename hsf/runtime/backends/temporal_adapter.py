"""Optional durable whole transactions; activities can execute more than once."""
from __future__ import annotations
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
from datetime import timedelta

try:
    from temporalio import activity, workflow
    from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
    from temporalio.exceptions import ApplicationError
    AVAILABLE = True
except ImportError:
    AVAILABLE = False

MAX_PAYLOAD = 65536
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")
_SHA = re.compile(r"[0-9a-f]{64}")


def require_temporal():
    if not AVAILABLE:
        raise RuntimeError("Temporal support requires the temporal optional dependency")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _validate_fields(fields):
    if not isinstance(fields, dict) or len(fields) > 256:
        raise ValueError("fields must be an object with at most 256 fields")
    for key, value in fields.items():
        if not isinstance(key, str) or not _NAME.fullmatch(key):
            raise ValueError("invalid field name")
        if type(value) not in (str, int, float, bool):
            raise ValueError("fields must contain primitive values")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("fields must be finite")


def validate_request(request):
    required = {"transaction_id", "artifact_name", "artifact_sha256", "fields"}
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("invalid request fields")
    for key in ("transaction_id", "artifact_name"):
        if not isinstance(request[key], str) or not _NAME.fullmatch(request[key]):
            raise ValueError(f"invalid {key}")
    if not isinstance(request["artifact_sha256"], str) or not _SHA.fullmatch(request["artifact_sha256"]):
        raise ValueError("invalid artifact_sha256")
    _validate_fields(request["fields"])
    encoded = _canonical(request)
    if len(encoded) > MAX_PAYLOAD:
        raise ValueError("request exceeds payload limit")
    return json.loads(encoded)


def request_digest(request):
    return hashlib.sha256(_canonical(validate_request(request))).hexdigest()


def _timeout(seconds):
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= 900:
        raise ValueError("timeout_seconds must be positive and at most 900")
    return seconds


def _child_env():
    names = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG")
    env = {key: os.environ[key] for key in names if key in os.environ}
    env.update(PYTHONHASHSEED="0", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    return env


_CHILD = r'''
import json, sys, tempfile
from pathlib import Path
from hsf.runtime import Orchestrator
from hsf.registry import RegistryError
from hsf.runtime.sandwich import strict_parse
try:
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536: raise ValueError("request too large")
    req = json.loads(raw)
    with tempfile.TemporaryDirectory(prefix="hsf-activity-") as scratch:
        runner = Orchestrator(Path(req["path"]), extractor=None,
            audit_path=Path(scratch) / "audit.jsonl",
            trusted_public_key=bytes.fromhex(req["public_key"]),
            expected_sha=req["sha"], allow_legacy_hmac=False, structured_only=True)
        try:
            if strict_parse(runner.ns["EXTRACT_SCHEMA"], req["fields"]) is None:
                raise ValueError("structured schema mismatch")
            result = runner.run_structured(req["fields"])
            output = {"status": result.status, "reason": result.reason}
        finally:
            runner.audit.close()
except (RegistryError, ValueError, TypeError, SyntaxError, PermissionError) as exc:
    output = {"error": str(exc)[:1000], "permanent": True}
except Exception as exc:
    output = {"error": type(exc).__name__, "permanent": False}
sys.stdout.write(json.dumps(output))
'''


async def _read_bounded(stream):
    data = bytearray()
    while chunk := await stream.read(4096):
        data.extend(chunk)
        if len(data) > MAX_PAYLOAD:
            raise ValueError("execution output exceeds payload limit")
    return bytes(data)


async def _communicate(child, payload):
    child.stdin.write(_canonical(payload))
    await child.stdin.drain()
    child.stdin.close()
    stdout, _ = await asyncio.gather(_read_bounded(child.stdout), _read_bounded(child.stderr))
    await child.wait()
    if child.returncode:
        raise RuntimeError("artifact execution child failed")
    return json.loads(stdout)


async def _execute_child(payload, timeout_seconds):
    root = Path(__file__).resolve().parents[3]
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-B", "-c", _CHILD, cwd=root, env=_child_env(),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    try:
        async with asyncio.timeout(timeout_seconds):
            return await _communicate(child, payload)
    finally:
        if child.returncode is None:
            child.kill()
        await child.wait()


async def _heartbeat_until_done(task):
    while not task.done():
        activity.heartbeat()
        await asyncio.sleep(0.5)


class ArtifactActivities:
    def __init__(self, registry_root, trusted_public_key, timeout_seconds=30):
        require_temporal()
        self.registry_root = Path(registry_root).resolve(strict=True)
        if not self.registry_root.is_dir() or not isinstance(trusted_public_key, bytes) or len(trusted_public_key) != 32:
            raise ValueError("configured registry directory and 32-byte public key required")
        self.public_key = trusted_public_key
        self.timeout_seconds = _timeout(timeout_seconds)

    def _payload(self, request):
        request = validate_request(request)
        filename = f"{request['artifact_name']}-{request['artifact_sha256'][:12]}.py"
        candidate = self.registry_root / filename
        if candidate.is_symlink():
            raise ValueError("artifact symlinks are forbidden")
        path = candidate.resolve()
        if path.parent != self.registry_root:
            raise ValueError("artifact escapes configured registry")
        payload = {"path": str(path), "sha": request["artifact_sha256"],
                   "public_key": self.public_key.hex(), "fields": request["fields"]}
        if len(_canonical(payload)) > MAX_PAYLOAD:
            raise ValueError("execution payload exceeds limit")
        return payload

    async def execute(self, request, timeout_seconds=None):
        try:
            effective_timeout = self.timeout_seconds if timeout_seconds is None else _timeout(timeout_seconds)
            if effective_timeout > self.timeout_seconds:
                raise ValueError("requested timeout exceeds configured worker maximum")
            task = asyncio.create_task(_execute_child(self._payload(request), effective_timeout))
            heartbeat = asyncio.create_task(_heartbeat_until_done(task))
            try:
                result = await task
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
        except (ValueError, TypeError, PermissionError) as exc:
            raise ApplicationError(str(exc), type="InvalidArtifactRequest", non_retryable=True) from exc
        return _checked_result(result, request)


def _checked_result(result, request):
    if "error" in result:
        raise ApplicationError(result["error"], type="ArtifactExecutionError",
                               non_retryable=result.get("permanent", False))
    if set(result) != {"status", "reason"} or result["status"] not in ("APPROVED", "DENIED", "HUMAN_REVIEW"):
        raise ApplicationError("invalid artifact result", non_retryable=True)
    if not isinstance(result["reason"], str):
        raise ApplicationError("invalid artifact reason", non_retryable=True)
    return {**result, "input_sha256": request_digest(request),
            "artifact_sha256": request["artifact_sha256"], "transaction_id": request["transaction_id"]}


if AVAILABLE:
    ArtifactActivities.execute = activity.defn(name="hsf.execute_artifact")(ArtifactActivities.execute)

    @workflow.defn
    class TemporalArtifactWorkflow:
        @workflow.run
        async def run(self, request, timeout_seconds=30):
            try:
                request = validate_request(request)
                timeout_seconds = _timeout(timeout_seconds)
            except (ValueError, TypeError) as exc:
                raise ApplicationError(str(exc), non_retryable=True) from exc
            return await workflow.execute_activity(
                "hsf.execute_artifact", args=[request, timeout_seconds],
                start_to_close_timeout=timedelta(seconds=timeout_seconds + 5),
                retry_policy=RetryPolicy(maximum_attempts=3),
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                heartbeat_timeout=timedelta(seconds=5))
else:
    class TemporalArtifactWorkflow:
        pass


def workflow_runner():
    """Do not reload the runtime parent's eager Orchestrator import in replay."""
    require_temporal()
    from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions
    restrictions = SandboxRestrictions.default.with_passthrough_modules("hsf.runtime.orchestrator")
    return SandboxedWorkflowRunner(restrictions=restrictions)


def create_worker(client, task_queue, activities):
    require_temporal()
    from temporalio.worker import Worker
    return Worker(client, task_queue=task_queue, workflows=[TemporalArtifactWorkflow],
                  activities=[activities.execute], workflow_runner=workflow_runner())


async def execute_transaction(client, task_queue, request, timeout_seconds=30):
    require_temporal()
    request = validate_request(request)
    timeout_seconds = _timeout(timeout_seconds)
    return await client.execute_workflow(
        TemporalArtifactWorkflow.run, args=[request, timeout_seconds],
        id=f"hsf:{request['transaction_id']}", task_queue=task_queue,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        execution_timeout=timedelta(seconds=3 * (timeout_seconds + 5) + 30))
