from __future__ import annotations

import json
import logging
import multiprocessing
import os
import socket
import sys
import time
import traceback
from types import SimpleNamespace

import pytest

import devflow.provider_probe as probe
from devflow.provider import (
    ChatCompletionsProvider,
    ProviderAuthenticationError,
    ProviderError,
    ProviderInputError,
    ProviderSettingsError,
    ProviderSettingsStore,
    ProviderTimeoutError,
)

SECRET = "sk-probe-secret-never-return-this"
_REAL_WORKER = probe._probe_worker


def _mock_post(self, body):
    assert self._api_key == SECRET
    assert json.loads(body)["messages"][1]["content"] == '{"operation":"connection_test"}'
    # Exercise suppression at Python, logger and native-descriptor levels.
    print(SECRET)
    print(SECRET, file=sys.stderr)
    logging.getLogger(__name__).error(SECRET)
    os.write(1, SECRET.encode())
    os.write(2, SECRET.encode())
    return json.dumps({"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": '{"ok":true}',
    }}]}).encode()


def _spawn_success(root, status):
    ChatCompletionsProvider._post = _mock_post
    _REAL_WORKER(root, status)


def _spawn_auth_error(root, status):
    def fail(self):
        error = ProviderAuthenticationError()
        # Even mutated safe exception objects must not be serialized.
        error.args = (SECRET,)
        error.code = SECRET
        error.public_message = SECRET
        raise error

    ChatCompletionsProvider.test = fail
    _REAL_WORKER(root, status)


def _spawn_unexpected_error(root, status):
    def fail(self):
        raise RuntimeError(SECRET)

    ChatCompletionsProvider.test = fail
    _REAL_WORKER(root, status)


def _spawn_system_exit(root, status):
    def fail(self):
        raise SystemExit(SECRET)

    ChatCompletionsProvider.test = fail
    _REAL_WORKER(root, status)


def _spawn_blocked_dns(root, status):
    def blocked(*args, **kwargs):
        while True:
            time.sleep(60)

    socket.getaddrinfo = blocked
    _REAL_WORKER(root, status)


def _spawn_blocked_http(root, status):
    def blocked(self):
        while True:
            time.sleep(60)

    ChatCompletionsProvider.test = blocked
    _REAL_WORKER(root, status)


def _spawn_result_then_hang(root, status):
    status.value = probe._SUCCESS
    while True:
        time.sleep(60)


def _spawn_crash(root, status):
    status.value = probe._SUCCESS
    os._exit(7)


def _spawn_missing_result(root, status):
    pass


def _spawn_unknown_result(root, status):
    status.value = 999999


def _daemon_caller(root, status):
    try:
        probe.probe_provider(root, timeout=1)
    except probe.ProviderProbeContextError:
        status.value = 1
    except BaseException:  # noqa: BLE001 - test child must not print unexpected errors
        status.value = -1


@pytest.fixture
def settings_root(tmp_path):
    root = tmp_path / "separate-secrets-volume"
    ProviderSettingsStore(root).save(
        "http://localhost:1/v1", "test-only", SECRET, allow_local_http=True,
    )
    return root


def _assert_safe(error):
    assert SECRET not in str(error)
    assert SECRET not in repr(error)
    assert SECRET not in error.code
    assert SECRET not in error.public_message
    assert SECRET not in "".join(traceback.format_exception(error))


def test_real_spawn_success_no_key_or_body_in_output(settings_root, monkeypatch, capfd):
    monkeypatch.setattr(probe, "_probe_worker", _spawn_success)
    before = {p.pid for p in multiprocessing.active_children()}
    assert probe.probe_provider(settings_root, timeout=10) == {"ok": True}
    assert {p.pid for p in multiprocessing.active_children()} == before
    captured = capfd.readouterr()
    assert SECRET not in captured.out + captured.err


@pytest.mark.parametrize("worker, expected", [
    (_spawn_auth_error, ProviderAuthenticationError),
    (_spawn_unexpected_error, probe.ProviderProbeError),
    (_spawn_system_exit, probe.ProviderProbeError),
    (_spawn_crash, probe.ProviderProbeError),
    (_spawn_missing_result, probe.ProviderProbeError),
    (_spawn_unknown_result, probe.ProviderProbeError),
])
def test_spawn_error_reconstructs_only_local_errors(settings_root, monkeypatch, capfd,
                                                  worker, expected):
    monkeypatch.setattr(probe, "_probe_worker", worker)
    before = {p.pid for p in multiprocessing.active_children()}
    with pytest.raises(expected) as caught:
        probe.probe_provider(settings_root, timeout=10)
    assert type(caught.value) is expected
    assert str(caught.value) == expected.public_message
    _assert_safe(caught.value)
    assert {p.pid for p in multiprocessing.active_children()} == before
    captured = capfd.readouterr()
    assert SECRET not in captured.out + captured.err


def test_real_worker_missing_settings_returns_safe_settings_error(tmp_path, capfd):
    with pytest.raises(ProviderSettingsError) as caught:
        probe.probe_provider(tmp_path / "missing", timeout=10)
    _assert_safe(caught.value)
    assert not (tmp_path / "missing").exists()
    assert SECRET not in capfd.readouterr().err


@pytest.mark.parametrize("worker", [_spawn_blocked_dns, _spawn_blocked_http,
                                    _spawn_result_then_hang])
def test_spawn_hard_timeout_terminates_and_reaps(settings_root, monkeypatch, worker):
    monkeypatch.setattr(probe, "_probe_worker", worker)
    before = {p.pid for p in multiprocessing.active_children()}
    started = time.monotonic()
    with pytest.raises(ProviderTimeoutError) as caught:
        probe.probe_provider(settings_root, timeout=1.0)
    elapsed = time.monotonic() - started
    assert 0.9 <= elapsed < 1 + 2 * probe.CLEANUP_TIMEOUT + 3
    _assert_safe(caught.value)
    assert {p.pid for p in multiprocessing.active_children()} == before


@pytest.mark.parametrize("error_type", probe._ERROR_TYPES)
def test_worker_preserves_every_known_error_type_without_serializing_error(monkeypatch, error_type):
    error = error_type()
    error.args = (SECRET,)
    error.code = error.public_message = SECRET

    def fail(self):
        raise error

    monkeypatch.setattr(ProviderSettingsStore, "provider", fail)
    value = probe._test_status("not-read")
    assert type(value) is int
    assert probe._ERROR_TYPES[value - 2] is error_type


@pytest.mark.parametrize("result", [{"ok": True, "key": SECRET}, {"ok": 1},
                                   {"ok": False}, SECRET, None])
def test_worker_does_not_return_arbitrary_provider_results(monkeypatch, result):
    monkeypatch.setattr(ProviderSettingsStore, "provider",
                        lambda self: SimpleNamespace(test=lambda: result))
    assert probe._test_status("not-read") == probe._FAILED


def test_unknown_domain_exception_subclass_does_not_supply_code(monkeypatch):
    class UnknownError(ProviderError):
        code = SECRET
        public_message = SECRET

    def fail(self):
        raise UnknownError()

    monkeypatch.setattr(ProviderSettingsStore, "provider", fail)
    assert probe._test_status("not-read") == probe._FAILED


class FakeProcess:
    def __init__(self, *, hang=False, resist_terminate=False, resist_kill=False,
                 start_error=None, join_error=None):
        self.pid = None
        self.exitcode = None
        self.hang = hang
        self.alive = False
        self.resist_terminate = resist_terminate
        self.resist_kill = resist_kill
        self.start_error = start_error
        self.join_error = join_error
        self.events = []

    def start(self):
        self.events.append("start")
        if self.start_error:
            raise self.start_error
        self.pid = 123
        self.alive = self.hang
        self.exitcode = None if self.alive else 0

    def join(self, timeout):
        self.events.append(("join", timeout))
        if self.join_error:
            error, self.join_error = self.join_error, None
            raise error

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.events.append("terminate")
        if not self.resist_terminate:
            self.alive = False

    def kill(self):
        self.events.append("kill")
        if not self.resist_kill:
            self.alive = False

    def close(self):
        self.events.append("close")


def fake_context(monkeypatch, process, result=probe._SUCCESS):
    calls = []
    status = SimpleNamespace(value=result)

    class Context:
        def Value(self, typecode, value, lock):
            calls.append(("value", typecode, value, lock))
            return status

        def Process(self, *, target, args, daemon):
            calls.append(("process", target, args, daemon))
            return process

    def context(method):
        assert method == "spawn"
        return Context()

    monkeypatch.setattr(probe.multiprocessing, "get_context", context)
    return calls, status


def test_parent_passes_only_path_and_integer_never_reads_key(settings_root, monkeypatch):
    process = FakeProcess()
    calls, status = fake_context(monkeypatch, process)

    def forbidden(self):
        pytest.fail("parent must not read settings")

    monkeypatch.setattr(ProviderSettingsStore, "provider", forbidden)
    monkeypatch.setattr(ProviderSettingsStore, "get", forbidden)
    assert probe.probe_provider(settings_root) == {"ok": True}
    assert calls[0] == ("value", "i", probe._PENDING, False)
    assert calls[1] == ("process", probe._probe_worker, (str(settings_root), status), True)
    assert SECRET not in repr(calls)
    assert process.events[-2:] == [("join", 0), "close"]


def test_timeout_escalates_terminate_join_kill_join_then_close(tmp_path, monkeypatch):
    process = FakeProcess(hang=True, resist_terminate=True)
    fake_context(monkeypatch, process)
    with pytest.raises(ProviderTimeoutError):
        probe.probe_provider(tmp_path, timeout=1)
    assert process.events[2:] == [
        "terminate", ("join", probe.CLEANUP_TIMEOUT),
        "kill", ("join", probe.CLEANUP_TIMEOUT), ("join", 0), "close",
    ]


def test_cleanup_failure_is_explicit_not_false_reaping_claim(tmp_path, monkeypatch):
    process = FakeProcess(hang=True, resist_terminate=True, resist_kill=True)
    fake_context(monkeypatch, process)
    with pytest.raises(probe.ProviderProbeCleanupError) as caught:
        probe.probe_provider(tmp_path, timeout=1)
    _assert_safe(caught.value)
    assert "kill" in process.events
    assert "close" not in process.events


def test_spawn_failure_sanitized_and_process_handle_closed(tmp_path, monkeypatch):
    process = FakeProcess(start_error=RuntimeError(SECRET))
    fake_context(monkeypatch, process)
    with pytest.raises(probe.ProviderProbeError) as caught:
        probe.probe_provider(tmp_path)
    _assert_safe(caught.value)
    assert process.events == ["start", "close"]


@pytest.mark.parametrize("error", [RuntimeError(SECRET), KeyboardInterrupt()])
def test_parent_wait_failure_still_terminates_child(tmp_path, monkeypatch, error):
    process = FakeProcess(hang=True, join_error=error)
    fake_context(monkeypatch, process)
    expected = KeyboardInterrupt if isinstance(error, KeyboardInterrupt) else probe.ProviderProbeError
    with pytest.raises(expected):
        probe.probe_provider(tmp_path)
    assert "terminate" in process.events
    assert process.events[-1] == "close"


def test_startup_time_is_subtracted_from_wait_budget(tmp_path, monkeypatch):
    process = FakeProcess()
    fake_context(monkeypatch, process)
    ticks = iter([100.0, 102.0, 103.0])
    monkeypatch.setattr(probe.time, "monotonic", lambda: next(ticks))
    assert probe.probe_provider(tmp_path, timeout=10) == {"ok": True}
    assert process.events[1] == ("join", 8.0)


def test_late_success_is_timeout_not_success(tmp_path, monkeypatch):
    process = FakeProcess()
    fake_context(monkeypatch, process)
    ticks = iter([100.0, 110.0, 111.0])
    monkeypatch.setattr(probe.time, "monotonic", lambda: next(ticks))
    with pytest.raises(ProviderTimeoutError):
        probe.probe_provider(tmp_path, timeout=10)
    assert process.events[1] == ("join", 0)
    assert process.events[-1] == "close"


@pytest.mark.parametrize("timeout", [0, -1, 121, True, None, "30", float("nan"), float("inf")])
def test_invalid_timeout_starts_no_worker(tmp_path, monkeypatch, timeout):
    def forbidden(*args):
        pytest.fail("invalid input must not start a worker")

    monkeypatch.setattr(probe.multiprocessing, "get_context", forbidden)
    with pytest.raises(ProviderInputError):
        probe.probe_provider(tmp_path, timeout=timeout)


def test_bad_path_error_is_sanitized():
    class BadPath:
        def __fspath__(self):
            raise ValueError(SECRET)

    with pytest.raises(probe.ProviderProbeError) as caught:
        probe.probe_provider(BadPath())
    _assert_safe(caught.value)


def test_cleanup_error_codes_are_stable():
    assert probe.ProviderProbeError.code == "provider_probe_error"
    assert probe.ProviderProbeCleanupError.code == "provider_probe_cleanup_error"
    assert probe.ProviderProbeContextError.code == "provider_probe_context_error"
    for error in (probe.ProviderProbeError(), probe.ProviderProbeCleanupError(),
                  probe.ProviderProbeContextError()):
        assert isinstance(error, ProviderError)
        assert str(error) == error.public_message


def test_daemon_parent_rejected_before_any_child_or_settings_access(monkeypatch, tmp_path):
    monkeypatch.setattr(probe.multiprocessing, "current_process",
                        lambda: SimpleNamespace(daemon=True))

    def forbidden(*args, **kwargs):
        pytest.fail("daemon parent must not create a context or read settings")

    monkeypatch.setattr(probe.multiprocessing, "get_context", forbidden)
    monkeypatch.setattr(ProviderSettingsStore, "provider", forbidden)
    with pytest.raises(probe.ProviderProbeContextError) as caught:
        probe.probe_provider(tmp_path)
    _assert_safe(caught.value)


def test_real_spawn_daemon_cannot_accidentally_nest_probe(tmp_path):
    context = multiprocessing.get_context("spawn")
    status = context.Value("i", 0, lock=False)
    process = context.Process(target=_daemon_caller, args=(str(tmp_path), status), daemon=True)
    try:
        process.start()
        process.join(10)
        assert not process.is_alive()
        assert process.exitcode == 0
        assert status.value == 1
    finally:
        probe._reap(process)
