from __future__ import annotations

import copy
import io
import json
import os
import socket
import stat
import threading
import traceback

import pytest

import devflow.provider as module
from devflow.models import CheckReport, CommandResult, FilePatchSet, TaskPlan
from devflow.provider import (
    ChatCompletionsProvider,
    ProviderAuthenticationError,
    ProviderError,
    ProviderInputError,
    ProviderInvalidJSONError,
    ProviderRateLimitError,
    ProviderRefusalError,
    ProviderResponseError,
    ProviderSettingsError,
    ProviderSettingsStore,
    ProviderTimeoutError,
    ProviderTransportError,
    ProviderTruncatedResponseError,
)

SECRET = "sk-private-test-only"
IDENTITY = {"run_id": "run-1", "patch_revision": 2, "base_workspace_revision": 3}
PLAN = {"goal": "Fix the function", "steps": ["Update source"],
        "files_to_modify": ["src/main.py"], "risks": []}
PATCH = {**IDENTITY, "files": [{"path": "src/main.py", "original": "old\n",
                              "modified": "new\n"}]}
REVIEW = {"run_id": "run-1", "patch_revision": 2, "summary": "Reviewed the change",
          "findings": [], "recommendation": "approve"}


def envelope(value=None, *, content=None, finish="stop", **message):
    return json.dumps({"choices": [{"finish_reason": finish, "message": {
        "role": "assistant", "content": content if content is not None else json.dumps(value),
        **message,
    }}]}).encode()


class Response:
    def __init__(self, data, status=200, headers=None):
        self.stream = io.BytesIO(data)
        self.status = status
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.closed = False
        self.read_sizes = []

    def getheader(self, key, default=None):
        return self.headers.get(key, default)

    def read1(self, size):
        self.read_sizes.append(size)
        return self.stream.read(size)

    def close(self):
        self.closed = True


@pytest.fixture
def transport(monkeypatch):
    responses, calls, connections = [], [], []

    class Connection:
        def __init__(self, host, port, **options):
            self.host, self.port, self.options = host, port, options
            self.sock = self
            self.timeouts = []
            self.closed = False
            connections.append(self)

        def settimeout(self, timeout):
            self.timeouts.append(timeout)

        def request(self, method, path, body, headers):
            calls.append((method, path, json.loads(body), headers))

        def getresponse(self):
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        def close(self):
            self.closed = True

    monkeypatch.setattr(module.http.client, "HTTPSConnection", Connection)
    monkeypatch.setattr(module.http.client, "HTTPConnection", Connection)
    return responses, calls, connections


@pytest.fixture
def client(transport):
    return ChatCompletionsProvider("https://provider.example/v1/", "test-model", SECRET)


def checks():
    command = CommandResult(passed=True, command=["test-double"], stdout="", stderr="",
                            duration_ms=0, exit_code=0)
    return CheckReport(run_id="run-1", patch_revision=2, lint=command, test=command, passed=True)


def safe_error(caught):
    assert SECRET not in str(caught.value)
    assert SECRET not in repr(caught.value)
    assert SECRET not in "".join(traceback.format_exception(caught.value))


def test_settings_round_trip_preserve_rotate_clear(tmp_path):
    store = ProviderSettingsStore(tmp_path / "separate-secrets-volume")
    assert store.get() == {"base_url": "", "model": "", "key_configured": False,
                           "allow_local_http": False}
    with pytest.raises(ProviderSettingsError):
        store.provider()
    saved = store.save("https://provider.example/v1/", "test-model", SECRET)
    assert saved == store.get() == {
        "base_url": "https://provider.example/v1", "model": "test-model",
        "key_configured": True, "allow_local_http": False,
    }
    path = store.root / "provider.json"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["api_key"] == SECRET
    assert SECRET not in repr(store.provider())
    store.save("https://provider.example/v1", "other-model")
    assert store.provider()._api_key == SECRET
    store.save("https://other.example/v1", "other-model")
    assert not store.get()["key_configured"]
    store.save("https://other.example/v1", "other-model", "replacement")
    assert store.provider()._api_key == "replacement"
    store.save("https://other.example/v1", "other-model", "")
    assert not store.get()["key_configured"]
    store.clear()
    store.clear()
    assert not path.exists()
    assert not store.get()["key_configured"]


def test_atomic_failure_preserves_old_record_and_cleans_temp(tmp_path, monkeypatch):
    store = ProviderSettingsStore(tmp_path)
    store.save("https://provider.example", "test-model", SECRET)
    before = (tmp_path / "provider.json").read_bytes()

    def fail(source, destination):
        if os.name == "posix":
            assert stat.S_IMODE(source.stat().st_mode) == 0o600
        raise OSError(SECRET)

    monkeypatch.setattr(module.os, "replace", fail)
    with pytest.raises(ProviderSettingsError) as caught:
        store.save("https://provider.example", "new-model", "new-key")
    safe_error(caught)
    assert (tmp_path / "provider.json").read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["provider.json"]


def test_permission_failure_writes_no_secret(tmp_path, monkeypatch):
    def fail(path):
        assert path.read_bytes() == b""
        raise OSError(SECRET)

    monkeypatch.setattr(module, "_restrict_file", fail)
    with pytest.raises(ProviderSettingsError) as caught:
        ProviderSettingsStore(tmp_path).save("https://provider.example", "m", SECRET)
    safe_error(caught)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("raw", [b"not JSON", b"{}", b'{"api_key":"secret"}',
                                 b"x" * (module.MAX_SETTINGS_BYTES + 1)],
                         ids=["invalid-json", "empty", "incomplete", "oversized"])
def test_corrupt_settings_fail_closed(tmp_path, raw):
    path = tmp_path / "provider.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    with pytest.raises(ProviderSettingsError):
        ProviderSettingsStore(tmp_path).get()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_world_readable_record_rejected(tmp_path):
    store = ProviderSettingsStore(tmp_path)
    store.save("https://provider.example", "m", SECRET)
    (tmp_path / "provider.json").chmod(0o644)
    with pytest.raises(ProviderSettingsError):
        store.get()


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL verification")
def test_windows_settings_have_protected_owner_only_dacl(tmp_path):
    import ctypes
    from ctypes import wintypes

    store = ProviderSettingsStore(tmp_path)
    store.save("https://provider.example", "m", SECRET)
    # Inspect the actual persisted ACL, rather than trusting chmod's emulation.
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    get_security = advapi.GetFileSecurityW
    get_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
                            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    get_security.restype = wintypes.BOOL
    needed = wintypes.DWORD()
    path = str(tmp_path / "provider.json")
    get_security(path, 4, None, 0, ctypes.byref(needed))
    buffer = ctypes.create_string_buffer(needed.value)
    assert get_security(path, 4, buffer, needed.value, ctypes.byref(needed))
    convert = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG)]
    convert.restype = wintypes.BOOL
    text = ctypes.c_void_p()
    assert convert(buffer, 1, 4, ctypes.byref(text), None)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        assert ctypes.wstring_at(text) == "D:P(A;;FA;;;OW)"
    finally:
        kernel.LocalFree(text)


def test_settings_reject_symlink_without_touching_target(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("sensitive target", encoding="utf-8")
    root = tmp_path / "secrets"
    root.mkdir()
    link = root / "provider.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable for this Windows account")
    store = ProviderSettingsStore(root)
    with pytest.raises(ProviderSettingsError):
        store.get()
    with pytest.raises(ProviderSettingsError):
        store.save("https://provider.example", "m", SECRET)
    assert target.read_text() == "sensitive target"


def test_bad_settings_update_preserves_config(tmp_path):
    store = ProviderSettingsStore(tmp_path)
    store.save("https://provider.example", "m", SECRET)
    before = (tmp_path / "provider.json").read_bytes()
    with pytest.raises(ProviderSettingsError) as caught:
        store.save(f"https://{SECRET}@provider.example", "m")
    safe_error(caught)
    assert (tmp_path / "provider.json").read_bytes() == before


@pytest.mark.parametrize("url", [
    "http://provider.example/v1", "http://192.168.1.1/v1", "http://0.0.0.0/v1",
    "http://127.1/v1", "http://2130706433/v1", "http://localhost.evil/v1",
    "ftp://localhost/v1", "https://user:password@provider.example/v1",
    "https://@provider.example/v1", "https://provider.example/v1?key=secret",
    "https://provider.example/v1?", "https://provider.example/v1#",
    "https://provider.example\\evil/v1", "https://provider.example\n/v1",
    "https://provider.example:99999/v1", "https://provider.example:0/v1",
    "https://provider.example:/v1", "https://[::1%25eth0]/v1", "//provider.example/v1",
])
def test_invalid_urls_rejected_even_with_http_opt_in(url):
    with pytest.raises(ProviderSettingsError):
        ChatCompletionsProvider(url, "m", SECRET, allow_local_http=True)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "127.0.0.2", "[::1]",
                                  "host.docker.internal"])
def test_local_http_requires_explicit_opt_in(host, transport):
    with pytest.raises(ProviderSettingsError):
        ChatCompletionsProvider(f"http://{host}:8080/v1", "m")
    client = ChatCompletionsProvider(f"http://{host}:8080/v1", "m", allow_local_http=True)
    transport[0].append(Response(envelope({"ok": True})))
    assert client.test() == {"ok": True}
    assert "Authorization" not in transport[1][0][3]
    assert "context" not in transport[2][0].options


@pytest.mark.parametrize("timeout", [0, -1, 121, float("inf"), float("nan"), True, "30"])
def test_timeout_bounds(timeout):
    with pytest.raises(ProviderSettingsError):
        ChatCompletionsProvider("https://provider.example", "m", timeout=timeout)


@pytest.mark.parametrize("kwargs", [
    {"model": ""}, {"model": " \t"}, {"model": "m\n"}, {"model": "x" * 257},
    {"api_key": SECRET + "\r\nInjected: yes"}, {"api_key": "x" * 4097},
    {"allow_local_http": "true"},
])
def test_invalid_settings_are_redacted(kwargs):
    with pytest.raises(ProviderSettingsError) as caught:
        ChatCompletionsProvider(**{"base_url": "https://provider.example", "model": "m",
                                   **kwargs})
    safe_error(caught)


def test_successful_plan_code_review_and_probe(client, transport):
    responses, calls, connections = transport
    responses.extend(Response(envelope(value)) for value in (PLAN, PATCH, REVIEW, {"ok": True}))
    plan = client.plan("Fix it", {"src/main.py": "old\n"})
    patch = client.code(plan, {"src/main.py": "old\n"}, **IDENTITY)
    report = client.review(plan, patch, checks())
    assert plan.model_dump() == PLAN
    assert patch.model_dump() == PATCH
    assert report.model_dump() == REVIEW
    assert client.test() == {"ok": True}
    assert len(calls) == 4
    for method, path, body, headers in calls:
        assert method == "POST"
        assert path == "/v1/chat/completions"
        assert body["response_format"] == {"type": "json_object"}
        assert body["stream"] is False
        assert body["max_completion_tokens"] == module.MAX_COMPLETION_TOKENS
        assert body["model"] == "test-model"
        assert "JSON" in body["messages"][0]["content"]
        assert SECRET not in json.dumps(body)
        assert headers["Authorization"] == "Bearer " + SECRET
        assert "tools" not in body
    assert json.loads(calls[1][2]["messages"][1]["content"])["originals"] == {
        "src/main.py": "old\n",
    }
    for connection in connections:
        assert connection.host == "provider.example"
        assert connection.options["context"].check_hostname
        assert connection.options["timeout"] == 30
        assert all(0 < value <= 30 for value in connection.timeouts)
        assert connection.closed


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 400, 401, 429, 500])
def test_http_errors_no_redirects_no_retries_no_body_leak(client, transport, status):
    response = Response(SECRET.encode(), status, {"Location": "https://evil.example"})
    transport[0].append(response)
    with pytest.raises(ProviderTransportError) as caught:
        client.test()
    safe_error(caught)
    assert len(transport[1]) == 1
    assert not response.read_sizes
    assert response.closed and transport[2][0].closed


@pytest.mark.parametrize("error", [TimeoutError(SECRET), OSError(SECRET), ValueError(SECRET)])
def test_transport_errors_sanitized(client, transport, error):
    transport[0].append(error)
    with pytest.raises(ProviderTransportError) as caught:
        client.test()
    safe_error(caught)
    assert len(transport[1]) == 1
    assert transport[2][0].closed


@pytest.mark.parametrize("raw", [
    b"not JSON", b"[]", b'{"choices":[],"choices":[]}', b'{"choices":[]}',
    b'{"error":{"message":"sk-private-test-only"}}', b"\xff",
    envelope(content='{"ok":true,"ok":true}'), envelope(content='{"ok":NaN}'),
    envelope(content='{"ok":Infinity}'), envelope(content='{"ok":1e999}'),
    envelope(content='{"ok":"\\ud800"}'), envelope(content="```json\n{}\n```"),
    envelope(content="{} trailing"), envelope(content="[]"), envelope(content=""),
    envelope({"ok": True}, finish="length"), envelope({"ok": True}, finish="content_filter"),
    envelope({"ok": True}, refusal=SECRET), envelope({"ok": True}, role="user"),
    envelope({"ok": True}, tool_calls=[{"id": "bad"}]),
    envelope({"ok": True}, function_call={"name": "bad"}),
    envelope({"ok": True, "extra": SECRET}), envelope({"ok": 1}), envelope({"ok": False}),
])
def test_invalid_responses_fail_closed(client, transport, raw):
    transport[0].append(Response(raw))
    with pytest.raises(ProviderResponseError) as caught:
        client.test()
    safe_error(caught)
    assert len(transport[1]) == 1


@pytest.mark.parametrize("headers", [
    {"Content-Type": "text/html"}, {"Content-Encoding": "gzip"},
    {"Content-Length": str(module.MAX_RESPONSE_BYTES + 1)}, {"Content-Length": "-1"},
    {"Content-Length": "1"},
])
def test_invalid_response_headers(client, transport, headers):
    transport[0].append(Response(envelope({"ok": True}), headers=headers))
    with pytest.raises(ProviderResponseError):
        client.test()


def test_response_stream_limit(client, transport, monkeypatch):
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 128)
    response = Response(b" " * 129)
    transport[0].append(response)
    with pytest.raises(ProviderResponseError):
        client.test()
    assert response.stream.tell() == 129
    assert response.closed


def test_request_limit_before_network(client, transport, monkeypatch):
    monkeypatch.setattr(module, "MAX_REQUEST_BYTES", 128)
    with pytest.raises(ProviderInputError):
        client.plan("task")
    assert not transport[1]


def test_deadline_stops_stream(client, transport, monkeypatch):
    ticks = iter([0, 1, 31])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    response = Response(envelope({"ok": True}))
    transport[0].append(response)
    with pytest.raises(ProviderTransportError):
        client.test()
    assert response.closed
    assert not response.read_sizes


@pytest.mark.parametrize("changes", [
    {"files_to_modify": []}, {"files_to_modify": ["../secret"]},
    {"files_to_modify": ["A.py", "a.py"]}, {"files_to_modify": ["a", "a/b"]},
    {"goal": " "}, {"steps": [" "]}, {"steps": [1]}, {"extra": SECRET},
])
def test_invalid_plans(client, transport, changes):
    transport[0].append(Response(envelope({**PLAN, **changes})))
    with pytest.raises(ProviderResponseError) as caught:
        client.plan("task")
    safe_error(caught)


@pytest.mark.parametrize("task, originals", [
    (" ", None), ("x" * 10001, None), ("task", {"../file": "text"}),
    ("task", {"a": "\x00"}), ("task", {"a": "x" * (module.MAX_FILE_BYTES + 1)}),
    ("task", {"A": "a", "a": "b"}), ("task", {"a": "a", "a/b": "b"}),
])
def test_invalid_inputs_before_network(client, transport, task, originals):
    with pytest.raises(ProviderInputError):
        client.plan(task, originals)
    assert not transport[1]


@pytest.mark.parametrize("changes", [
    {"run_id": "another"}, {"patch_revision": 3}, {"base_workspace_revision": 4},
    {"patch_revision": "2"}, {"patch_revision": True}, {"files": []}, {"extra": SECRET},
    {"files": [{"path": "src/main.py", "original": "wrong", "modified": "new"}]},
    {"files": [{"path": "other.py", "original": None, "modified": "new"}]},
    {"files": [{"path": "../bad", "original": None, "modified": "new"}]},
    {"files": [{"path": "src/main.py", "original": "old\n", "modified": "old\n"}]},
    {"files": PATCH["files"] * 2},
])
def test_invalid_patch_identity_paths_originals_and_noops(client, transport, changes):
    transport[0].append(Response(envelope({**PATCH, **changes})))
    with pytest.raises(ProviderResponseError) as caught:
        client.code(TaskPlan(**PLAN), {"src/main.py": "old\n"}, **IDENTITY)
    safe_error(caught)


def test_creation_deletion_and_unicode(client, transport):
    plan = TaskPlan(**{**PLAN, "files_to_modify": ["new.py", "old.py"]})
    result = {**IDENTITY, "files": [
        {"path": "new.py", "original": None, "modified": "# café\n"},
        {"path": "old.py", "original": "old", "modified": None},
    ]}
    transport[0].append(Response(envelope(result)))
    assert client.code(plan, {"old.py": "old"}, **IDENTITY).model_dump() == result


@pytest.mark.parametrize("changes", [
    {"run_id": "other"}, {"patch_revision": 3}, {"patch_revision": "2"},
    {"summary": " "}, {"recommendation": "maybe"}, {"extra": SECRET},
    {"findings": [{"severity": "error", "message": "bad", "path": "../bad"}]},
    {"findings": [{"severity": "error", "message": "bad", "path": "other.py"}]},
    {"findings": [{"severity": "error", "message": " ", "path": None}]},
])
def test_invalid_reviews(client, transport, changes):
    transport[0].append(Response(envelope({**REVIEW, **changes})))
    with pytest.raises(ProviderResponseError) as caught:
        client.review(TaskPlan(**PLAN), FilePatchSet(**PATCH), checks())
    safe_error(caught)


def test_check_identity_mismatch_before_network(client, transport):
    report = checks().model_copy(update={"run_id": "other"})
    with pytest.raises(ProviderInputError):
        client.review(TaskPlan(**PLAN), FilePatchSet(**PATCH), report)
    assert not transport[1]


def test_no_mutation_of_callers(client, transport):
    plan = TaskPlan(**PLAN)
    original = {"src/main.py": "old\n"}
    before = copy.deepcopy((plan, original))
    transport[0].append(Response(envelope(PATCH)))
    client.code(plan, original, **IDENTITY)
    assert (plan, original) == before


def test_provider_errors_integrate_with_domain_errors():
    error = ProviderResponseError()
    assert isinstance(error, ProviderError)
    assert str(error) == error.public_message


def test_paths_cannot_alias_existing_workspace_files(client, transport):
    transport[0].append(Response(envelope({**PLAN, "files_to_modify": ["SRC/main.py"]})))
    with pytest.raises(ProviderResponseError):
        client.plan("task", {"src/main.py": "old"})
    with pytest.raises(ProviderInputError):
        client.code(TaskPlan(**PLAN), {"src": "file not directory"}, **IDENTITY)
    assert len(transport[1]) == 1


def test_invalid_run_identity_is_not_sent(client, transport):
    with pytest.raises(ProviderInputError):
        client.code(TaskPlan(**PLAN), {}, **{**IDENTITY, "run_id": " "})
    with pytest.raises(ProviderInputError):
        client.code(TaskPlan(**PLAN), {}, **{**IDENTITY, "patch_revision": True})
    assert not transport[1]


def test_review_accepts_file_and_global_findings(client, transport):
    report = {**REVIEW, "recommendation": "revise", "findings": [
        {"severity": "warning", "message": "Check this change", "path": "src/main.py"},
        {"severity": "info", "message": "Human review required", "path": None},
    ]}
    transport[0].append(Response(envelope(report)))
    result = client.review(TaskPlan(**PLAN), FilePatchSet(**PATCH), checks())
    assert result.model_dump() == report


@pytest.mark.parametrize("error_type, code, message, parent", [
    (ProviderAuthenticationError, "provider_authentication_error",
     "provider denied access; check the API key and model permissions", ProviderTransportError),
    (ProviderRateLimitError, "provider_rate_limit_error",
     "provider rate or quota limit reached; check usage or try again later", ProviderTransportError),
    (ProviderTimeoutError, "provider_timeout_error",
     "provider request timed out; check provider availability or try again later",
     ProviderTransportError),
    (ProviderRefusalError, "provider_refusal_error",
     "provider declined the request or filtered the response; revise the task", ProviderResponseError),
    (ProviderTruncatedResponseError, "provider_truncated_response_error",
     "provider response was cut short; reduce the task size or output requested",
     ProviderResponseError),
    (ProviderInvalidJSONError, "provider_invalid_json_error",
     "provider returned invalid JSON; a complete JSON object is required", ProviderResponseError),
])
def test_categorized_error_public_fields(error_type, code, message, parent):
    error = error_type()
    assert isinstance(error, parent)
    assert isinstance(error, module.DevFlowError)
    assert error.code == code
    assert error.public_message == str(error) == message
    assert error.args == (message,)


@pytest.mark.parametrize("status, expected", [
    (401, ProviderAuthenticationError), (403, ProviderAuthenticationError),
    (429, ProviderRateLimitError), (408, ProviderTimeoutError), (504, ProviderTimeoutError),
    (302, ProviderTransportError), (400, ProviderTransportError), (500, ProviderTransportError),
])
def test_http_error_categories_ignore_untrusted_body_and_headers(client, transport, status, expected):
    response = Response(SECRET.encode(), status, {
        "Retry-After": SECRET, "WWW-Authenticate": SECRET, "Location": SECRET,
        "Content-Type": "text/html", "Content-Length": "invalid",
    })
    transport[0].append(response)
    with pytest.raises(expected) as caught:
        client.test()
    assert type(caught.value) is expected
    safe_error(caught)
    assert SECRET not in caught.value.public_message
    assert not response.read_sizes
    assert response.closed and transport[2][0].closed
    assert len(transport[1]) == 1


@pytest.mark.parametrize("operation", ["plan", "code", "review", "test"])
@pytest.mark.parametrize("raw, expected", [
    (envelope(content="not-json", refusal=SECRET), ProviderRefusalError),
    (envelope(content="not-json", finish="content_filter"), ProviderRefusalError),
    (envelope(content='{"partial":', finish="length"), ProviderTruncatedResponseError),
    (envelope({"ok": True}, finish="length"), ProviderTruncatedResponseError),
    (envelope(content='{"partial":'), ProviderInvalidJSONError),
    (envelope(content=SECRET), ProviderInvalidJSONError),
    (SECRET.encode(), ProviderInvalidJSONError),
    (envelope(content='{"x":1,"x":2}'), ProviderInvalidJSONError),
    (envelope(content='{"x":NaN}'), ProviderInvalidJSONError),
    (envelope(content='{"x":"\\ud800"}'), ProviderInvalidJSONError),
    (envelope(content="[]"), ProviderResponseError),
    (envelope({}), ProviderResponseError),
    (json.dumps({"error": {"code": "rate_limit_exceeded", "message": SECRET}}).encode(),
     ProviderResponseError),
])
def test_error_categories_survive_all_public_methods(client, transport, operation, raw, expected):
    transport[0].append(Response(raw))
    calls = {
        "plan": lambda: client.plan("task"),
        "code": lambda: client.code(TaskPlan(**PLAN), {"src/main.py": "old\n"}, **IDENTITY),
        "review": lambda: client.review(TaskPlan(**PLAN), FilePatchSet(**PATCH), checks()),
        "test": client.test,
    }
    with pytest.raises(expected) as caught:
        calls[operation]()
    assert type(caught.value) is expected
    safe_error(caught)
    assert len(transport[1]) == 1
    assert transport[2][0].closed


@pytest.mark.parametrize("stage", ["request", "headers", "body"])
def test_socket_timeout_category_at_each_io_stage(client, transport, monkeypatch, stage):
    response = Response(envelope({"ok": True}))
    transport[0].append(response)

    def timeout(*args, **kwargs):
        raise TimeoutError(SECRET)  # socket.timeout is this same type on supported Python.

    connection_type = module.http.client.HTTPSConnection
    if stage == "request":
        original = connection_type.request

        def request(self, *args, **kwargs):
            original(self, *args, **kwargs)
            timeout()

        monkeypatch.setattr(connection_type, "request", request)
    elif stage == "headers":
        monkeypatch.setattr(connection_type, "getresponse", timeout)
    else:
        monkeypatch.setattr(response, "read1", timeout)
    with pytest.raises(ProviderTimeoutError) as caught:
        client.test()
    assert type(caught.value) is ProviderTimeoutError
    safe_error(caught)
    assert len(transport[1]) == 1
    assert transport[2][0].closed
    if stage == "body":
        assert response.closed


def test_incomplete_http_body_is_truncation_not_invalid_json(client, transport, monkeypatch):
    response = Response(b"")
    transport[0].append(response)

    def incomplete(_size):
        raise module.http.client.IncompleteRead(SECRET.encode(), 500)

    monkeypatch.setattr(response, "read1", incomplete)
    with pytest.raises(ProviderTruncatedResponseError) as caught:
        client.test()
    safe_error(caught)
    assert response.closed and transport[2][0].closed
    assert len(transport[1]) == 1


def test_premature_eof_is_truncation_even_if_json_is_valid(client, transport):
    raw = envelope({"ok": True})
    response = Response(raw, headers={"Content-Length": str(len(raw) + 1)})
    transport[0].append(response)
    with pytest.raises(ProviderTruncatedResponseError):
        client.test()
    assert response.closed
    assert len(transport[1]) == 1


@pytest.mark.parametrize("length", ["-1", "not-a-number", "1.0", "+3", "١٢"])
def test_bad_content_length_is_response_error_not_transport_error(client, transport, length):
    transport[0].append(Response(envelope({"ok": True}), headers={"Content-Length": length}))
    with pytest.raises(ProviderResponseError) as caught:
        client.test()
    assert type(caught.value) is ProviderResponseError


def test_cooperative_deadline_has_timeout_category(client, transport, monkeypatch):
    ticks = iter([0, 1, 31])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    response = Response(envelope({"ok": True}))
    transport[0].append(response)
    with pytest.raises(ProviderTimeoutError):
        client.test()
    assert response.closed
    assert not response.read_sizes


def test_dns_block_is_not_interrupted_by_socket_timeout(monkeypatch):
    # Use the real stdlib connect path, but replace DNS: no network is contacted.
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    errors, lookups = [], []

    def blocked_dns(*args, **kwargs):
        lookups.append(args)
        entered.set()
        release.wait(5)  # Test cleanup is bounded, even if an assertion fails.
        raise socket.gaierror(SECRET)

    monkeypatch.setattr(socket, "getaddrinfo", blocked_dns)
    provider = ChatCompletionsProvider(
        "http://localhost/v1", "m", SECRET, allow_local_http=True, timeout=0.01,
    )

    def probe():
        try:
            provider.test()
        except ProviderError as error:
            errors.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    try:
        assert entered.wait(2)
        # The synchronous probe remains blocked after its configured timeout.
        assert not finished.wait(0.05)
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    assert len(lookups) == len(errors) == 1
    assert type(errors[0]) is ProviderTransportError
    assert SECRET not in "".join(traceback.format_exception(errors[0]))


def test_blocked_header_call_requires_external_hard_cancellation(client, transport, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    response = Response(envelope({"ok": True}))
    errors = []
    client.timeout = 0.01

    def blocked_headers(self):
        entered.set()
        release.wait(5)
        return response

    monkeypatch.setattr(module.http.client.HTTPSConnection, "getresponse", blocked_headers)

    def probe():
        try:
            client.test()
        except ProviderError as error:
            errors.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    try:
        assert entered.wait(2)
        assert not finished.wait(0.05)
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    # After the blocking call returns, the cooperative deadline rejects success.
    assert len(errors) == 1 and type(errors[0]) is ProviderTimeoutError
    assert len(transport[1]) == 1
    assert response.closed and transport[2][0].closed
