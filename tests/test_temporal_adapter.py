"""Local contracts plus explicitly enabled real Temporal test-server controls."""
import asyncio
import os
from pathlib import Path

import pytest

from hsf.runtime.backends import temporal_adapter as adapter


def _request(sha="a" * 64, transaction="control"):
    return {"transaction_id": transaction, "artifact_name": "policy",
            "artifact_sha256": sha, "fields": {"enabled": True}}


@pytest.mark.parametrize("change", [
    {"artifact_name": "../escape"}, {"transaction_id": "x/y"},
    {"artifact_sha256": "bad"}, {"fields": {"nested": {}}},
    {"fields": {"bad": float("nan")}}, {"fields": {"bad": None}},
    {"fields": {"bad": "x" * adapter.MAX_PAYLOAD}}, {"public_key": "untrusted"},
])
def test_reject_invalid_request(change):
    with pytest.raises(ValueError):
        adapter.validate_request({**_request(), **change})


def test_identity_is_canonical_and_input_bound():
    request = _request()
    reverse = dict(reversed(list(request.items())))
    assert adapter.request_digest(request) == adapter.request_digest(reverse)
    reverse["fields"] = {"enabled": False}
    assert adapter.request_digest(request) != adapter.request_digest(reverse)
    assert adapter.validate_request(request) == request
    assert adapter.validate_request(request) is not request


def test_no_provider_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sentinel")
    monkeypatch.setenv("HSF_EXTRACTOR_KEY", "sentinel")
    monkeypatch.setenv("PYTHONPATH", "sentinel")
    env = adapter._child_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "HSF_EXTRACTOR_KEY" not in env
    assert "PYTHONPATH" not in env


def test_optional_dependency_error(monkeypatch):
    monkeypatch.setattr(adapter, "AVAILABLE", False)
    with pytest.raises(RuntimeError, match="optional dependency"):
        adapter.create_worker(None, "queue", None)


@pytest.mark.skipif(not adapter.AVAILABLE, reason="Temporal optional dependency absent")
def test_workflow_sandbox_prepares():
    from temporalio.workflow import _Definition
    async def prepare():
        adapter.workflow_runner().prepare_workflow(_Definition.must_from_class(adapter.TemporalArtifactWorkflow))
    asyncio.run(prepare())


@pytest.mark.parametrize("timeout", [0, -1, 901, float("nan"), True])
def test_timeout_bounds(timeout):
    with pytest.raises(ValueError):
        adapter._timeout(timeout)


def _signed(tmp_path, artifact_source):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from hsf.registry import store_artifact
    key = Ed25519PrivateKey.generate()
    path, sha = store_artifact(artifact_source, tmp_path, "policy", signing_key=key.private_bytes_raw())
    request = _request(sha)
    request["fields"] = {"has_t2d_diagnosis": True, "current_a1c": 7.2, "bmi": 28.0}
    return path, key.public_key().public_bytes_raw(), request


@pytest.mark.skipif(not adapter.AVAILABLE, reason="Temporal optional dependency absent")
def test_signed_child_executes_and_rejects_tamper(tmp_path, artifact_source):
    path, public, request = _signed(tmp_path, artifact_source)
    activities = adapter.ArtifactActivities(tmp_path, public)
    result = asyncio.run(adapter._execute_child(activities._payload(request), 10))
    assert result == {"status": "APPROVED", "reason": "T2D Diagnosis"}
    malformed = {**request, "fields": {"enabled": True}}
    result = asyncio.run(adapter._execute_child(activities._payload(malformed), 10))
    assert result == {"error": "structured schema mismatch", "permanent": True}
    path.write_bytes(path.read_bytes() + b"\n# tampered\n")
    result = asyncio.run(adapter._execute_child(activities._payload(request), 10))
    assert result["permanent"] is True


@pytest.mark.skipif(not adapter.AVAILABLE, reason="Temporal optional dependency absent")
def test_registry_containment(tmp_path):
    activities = adapter.ArtifactActivities(tmp_path, b"x" * 32)
    payload = activities._payload(_request())
    assert Path(payload["path"]).parent == tmp_path.resolve()
    assert Path(payload["path"]).name == "policy-aaaaaaaaaaaa.py"
    with pytest.raises(ValueError):
        activities._payload({**_request(), "artifact_name": "../escape"})


def test_child_cancel_and_timeout_reap(monkeypatch):
    observed = []
    original = asyncio.create_subprocess_exec

    async def launch(*args, **kwargs):
        child = await original(*args, **kwargs)
        observed.append(child)
        return child

    monkeypatch.setattr(adapter.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(adapter, "_CHILD", "import time; time.sleep(60)")

    async def controls():
        with pytest.raises(TimeoutError):
            await adapter._execute_child({}, 0.1)
        task = asyncio.create_task(adapter._execute_child({}, 10))
        while len(observed) < 2:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(child.returncode is not None for child in observed)

    asyncio.run(controls())


@pytest.mark.skipif(not adapter.AVAILABLE, reason="Temporal optional dependency absent")
def test_activity_timeout_uses_request_and_enforces_worker_cap(tmp_path, monkeypatch):
    import time
    from temporalio.exceptions import ApplicationError
    observed = []
    original = asyncio.create_subprocess_exec

    async def launch(*args, **kwargs):
        child = await original(*args, **kwargs)
        observed.append(child)
        return child

    monkeypatch.setattr(adapter.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(adapter, "_CHILD", "import time; time.sleep(60)")
    monkeypatch.setattr(adapter.activity, "heartbeat", lambda: None)

    async def controls():
        generous = adapter.ArtifactActivities(tmp_path, b"x" * 32, timeout_seconds=900)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await generous.execute(_request(), 1)
        assert time.monotonic() - started < 4
        assert len(observed) == 1 and observed[0].returncode is not None
        restricted = adapter.ArtifactActivities(tmp_path, b"x" * 32, timeout_seconds=1)
        with pytest.raises(ApplicationError, match="worker maximum") as failure:
            await restricted.execute(_request(), 30)
        assert failure.value.non_retryable
        assert len(observed) == 1
        # Direct use without a supplied timeout retains the operator's cap.
        with pytest.raises(TimeoutError):
            await restricted.execute(_request())
        assert len(observed) == 2 and observed[-1].returncode is not None

    asyncio.run(controls())


_LIVE = adapter.AVAILABLE and os.environ.get("HSF_TEMPORAL_TEST_SERVER") == "1"


@pytest.mark.skipif(not _LIVE, reason="set HSF_TEMPORAL_TEST_SERVER=1 for actual controlled server")
def test_temporal_server_execution_replay_retry_cancel_restart(tmp_path, artifact_source):
    asyncio.run(asyncio.wait_for(_server_controls(tmp_path, artifact_source), timeout=120))


async def _server_controls(tmp_path, artifact_source):
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Replayer, Worker
    from temporalio import activity
    from temporalio.exceptions import ApplicationError
    path, public, request = _signed(tmp_path, artifact_source)
    activities = adapter.ArtifactActivities(tmp_path, public)
    cache = os.environ.get("HSF_TEMPORAL_TEST_CACHE")
    if cache:
        # The SDK writes its executable here but does not create parent folders.
        Path(cache).mkdir(parents=True, exist_ok=True)
    # Use the same real development server on every OS; virtual time can hide
    # scheduling differences in process cancellation and worker restart controls.
    print("Temporal: starting development server", flush=True)
    async with await WorkflowEnvironment.start_local(download_dest_dir=cache) as env:
        print("Temporal: signed execution and replay", flush=True)
        async with adapter.create_worker(env.client, "execution", activities):
            result = await adapter.execute_transaction(env.client, "execution", request)
            assert result["status"] == "APPROVED"
            handle = env.client.get_workflow_handle("hsf:control")
            history = await handle.fetch_history()
            await Replayer(workflows=[adapter.TemporalArtifactWorkflow],
                           workflow_runner=adapter.workflow_runner()).replay_workflow(history)
        print("Temporal: retry", flush=True)
        await _retry_control(env, Worker, activity, ApplicationError)
        print("Temporal: permanent rejection", flush=True)
        await _permanent_control(env, activities, path, request)
        print("Temporal: child cancellation", flush=True)
        await _real_child_cancel_control(env, activities, request)
        print("Temporal: worker recovery", flush=True)
        await _cancel_restart_control(env, Worker, activity, activities, request)


async def _retry_control(env, Worker, activity, ApplicationError):
    attempts = []

    @activity.defn(name="hsf.execute_artifact")
    async def transient(request, timeout_seconds):
        attempts.append(activity.info().attempt)
        if len(attempts) < 3:
            raise ApplicationError("transient")
        return {"status": "APPROVED"}

    async with Worker(env.client, task_queue="retry", workflows=[adapter.TemporalArtifactWorkflow],
                      activities=[transient], workflow_runner=adapter.workflow_runner()):
        assert (await adapter.execute_transaction(env.client, "retry", _request(transaction="retry")))["status"] == "APPROVED"
    assert attempts == [1, 2, 3]


async def _permanent_control(env, activities, path, request):
    signature = path.with_suffix(".py.sig")
    original = signature.read_bytes()
    signature.write_bytes(b"invalid")
    try:
        async with adapter.create_worker(env.client, "permanent", activities):
            with pytest.raises(Exception) as failure:
                await adapter.execute_transaction(env.client, "permanent",
                    {**request, "transaction_id": "permanent"})
            assert "signature" in str(failure.value.cause.cause).lower()
            history = await env.client.get_workflow_handle("hsf:permanent").fetch_history()
            from temporalio.api.enums.v1 import EventType
            failures = [event for event in history.events
                        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED]
            assert len(failures) == 1
            assert failures[0].activity_task_failed_event_attributes.failure.application_failure_info.non_retryable
    finally:
        signature.write_bytes(original)


async def _real_child_cancel_control(env, activities, request):
    observed = []
    launched = asyncio.Event()
    original = asyncio.create_subprocess_exec

    async def launch(*args, **kwargs):
        child = await original(*args, **kwargs)
        observed.append(child)
        launched.set()
        return child

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(adapter, "_CHILD", "import time; time.sleep(60)")
        patch.setattr(adapter.asyncio, "create_subprocess_exec", launch)
        async with adapter.create_worker(env.client, "real-cancel", activities):
            handle = await env.client.start_workflow(adapter.TemporalArtifactWorkflow.run,
                args=[{**request, "transaction_id": "real-cancel"}, 30],
                id="hsf:real-cancel", task_queue="real-cancel")
            await asyncio.wait_for(launched.wait(), 20)
            await handle.cancel()
            with pytest.raises(Exception):
                await handle.result()
            assert len(observed) == 1
            assert observed[0].returncode is not None


async def _cancel_restart_control(env, Worker, activity, activities, request):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    @activity.defn(name="hsf.execute_artifact")
    async def waiting(request, timeout_seconds):
        started.set()
        try:
            while True:
                activity.heartbeat()
                await asyncio.sleep(0.1)
        finally:
            cancelled.set()

    async with Worker(env.client, task_queue="cancel", workflows=[adapter.TemporalArtifactWorkflow],
                      activities=[waiting], workflow_runner=adapter.workflow_runner()):
        handle = await env.client.start_workflow(adapter.TemporalArtifactWorkflow.run,
            args=[_request(transaction="cancel"), 30], id="hsf:cancel", task_queue="cancel")
        await asyncio.wait_for(started.wait(), 20)
        await handle.cancel()
        with pytest.raises(Exception):
            await handle.result()
        await asyncio.wait_for(cancelled.wait(), 10)
    # Persist a scheduled activity, stop its workflow worker, resume on a new worker.
    from temporalio.api.enums.v1 import EventType
    async with Worker(env.client, task_queue="restart", workflows=[adapter.TemporalArtifactWorkflow],
                      workflow_runner=adapter.workflow_runner()):
        pending = await env.client.start_workflow(adapter.TemporalArtifactWorkflow.run,
            args=[{**request, "transaction_id": "restart"}, 30], id="hsf:restart", task_queue="restart")
        async with asyncio.timeout(20):
            while True:
                history = await pending.fetch_history()
                if any(event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED for event in history.events):
                    break
                await asyncio.sleep(0.05)
    async with adapter.create_worker(env.client, "restart", activities):
        assert (await pending.result())["status"] == "APPROVED"
