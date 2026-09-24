from __future__ import annotations

import asyncio
import json
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from functools import partial
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from support import PassingRunner

from devflow.database import Database
from devflow.events import CursorError, EventStream, EventStreamResponse
from devflow.main import create_app as _create_app
from devflow.models import FilePatchSet

create_app = partial(_create_app, asynchronous=False, mode="mock", security_enabled=False)


def seed(database):
    database.initialize()
    database.create_run(
        run_id="run-1", thread_id="run-1", patch_id="patch-1", patch_revision=1,
        candidate_dir="unused", patch={"files": []}, task="example",
    )


@pytest.fixture
def database(tmp_path):
    database = Database(tmp_path / "events.sqlite")
    seed(database)
    return database


def append(database):
    database.append_node_event("run-1", "review", "node.started", 1)


def test_concurrent_sequences_rollback_and_reopen(database):
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: append(database), range(30)))
    with pytest.raises(RuntimeError), database.transaction() as connection:
        database._append_event(
            connection, run_id="run-1", event_type="rolled.back", node=None, payload={},
        )
        raise RuntimeError("rollback")
    reopened = Database(database.path)
    reopened.initialize()
    append(reopened)
    rows = reopened.list_events("run-1")
    assert [row["seq"] for row in rows] == list(range(1, 33))
    assert "rolled.back" not in [row["type"] for row in rows]


def test_snapshot_is_consistent_during_a_concurrent_commit(database, monkeypatch):
    connect = database.connect
    written = False

    def snapshot_connection():
        connection = connect()

        def interleave(sql):
            nonlocal written
            if "SELECT patch_revision" in sql and not written:
                written = True
                # Use a separate Database to avoid recursively installing trace.
                Database(database.path).fail_run_start("run-1")

        connection.set_trace_callback(interleave)
        return connection

    monkeypatch.setattr(database, "connect", snapshot_connection)
    before = database.snapshot("run-1")
    assert written
    assert (before["status"], before["latest_seq"]) == ("RUNNING", 1)
    after = database.snapshot("run-1")
    assert (after["status"], after["latest_seq"]) == ("FAILED", 2)


def test_artifacts_and_patch_advance_snapshot_watermark_atomically(database, monkeypatch):
    database.save_artifact("run-1", "plan", {"goal": "test"})
    assert database.snapshot("run-1")["latest_seq"] == 2
    patch = FilePatchSet(run_id="run-1", patch_revision=1, base_workspace_revision=0, files=[])
    database.save_generated_patch(patch)
    assert database.snapshot("run-1")["latest_seq"] == 3

    def fail(*args, **kwargs):
        raise RuntimeError("event insert failed")

    monkeypatch.setattr(database, "_append_event", fail)
    with pytest.raises(RuntimeError):
        database.save_artifact("run-1", "review_report", {"summary": "not committed"})
    assert "review_report" not in database.snapshot("run-1")
    assert database.snapshot("run-1")["latest_seq"] == 3


def test_pages_close_connections_and_expire_without_deleting_events(database, monkeypatch):
    for _ in range(5):
        append(database)
    closed = []

    class TrackedConnection(sqlite3.Connection):
        def close(self):
            closed.append(self)
            super().close()

    def connect():
        connection = sqlite3.connect(database.path, isolation_level=None, factory=TrackedConnection)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(database, "connect", connect)
    stream = EventStream(database, replay_window=3, page_size=2)
    assert [e["seq"] for e in stream.read_page("run-1", None).events] == [4, 5]
    assert [e["seq"] for e in stream.read_page("run-1", 3).events] == [4, 5]
    with pytest.raises(CursorError) as error:
        stream.read_page("run-1", 2)
    assert error.value.status == 410
    assert stream.read_page("run-1", 6).events == []
    with pytest.raises(CursorError):
        stream.read_page("missing", 0)
    assert database.snapshot("run-1")["latest_seq"] == 6
    assert len(closed) == 6
    with closing(connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 6


def test_expiry_during_stream_is_explicit_control_frame(database):
    async def run():
        stream = EventStream(database, replay_window=2, page_size=1)
        frames = stream.frames("run-1", stream.read_page("run-1", 0))
        assert await anext(frames) == "retry: 1000\n\n"
        assert "id: 1\n" in await anext(frames)
        for _ in range(3):
            append(database)
        reset = await anext(frames)
        assert reset.startswith("event: stream.reset\n")
        assert "cursor_expired" in reset
        assert "id:" not in reset
        with pytest.raises(StopAsyncIteration):
            await anext(frames)
        assert stream.active_connections == 0

    asyncio.run(run())


def test_corrupt_payload_ends_established_stream_with_error_frame(database):
    async def run():
        stream = EventStream(database)
        page = stream.read_page("run-1", 1)
        frames = stream.frames("run-1", page)
        assert await anext(frames) == "retry: 1000\n\n"
        with database.transaction() as connection:
            event_seq = database._append_event(
                connection, run_id="run-1", event_type="node.started",
                node="review", payload={},
            )
            connection.execute(
                "UPDATE run_events SET payload_json = '{' WHERE run_id = ? AND seq = ?",
                ("run-1", event_seq),
            )

        error_frame = await anext(frames)

        assert error_frame.startswith("event: stream.error\n")
        assert "id:" not in error_frame
        assert json.loads(error_frame.split("data: ", 1)[1]) == {
            "error": {
                "code": "event_store_corrupt",
                "message": "stored run event payload is invalid",
                "event_seq": event_seq,
            }
        }
        with pytest.raises(StopAsyncIteration):
            await anext(frames)
        assert stream.active_connections == 0

    asyncio.run(run())


def test_asgi_send_failure_closes_generator(database):
    async def run():
        stream = EventStream(database)
        response = EventStreamResponse(stream.frames("run-1", stream.read_page("run-1", 0)))

        async def send(message):
            if message["type"] == "http.response.body":
                raise OSError("peer disconnected")

        async def receive():
            await asyncio.Future()

        from starlette.requests import ClientDisconnect
        with pytest.raises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert stream.active_connections == 0

    asyncio.run(run())


def test_http_cursor_errors_before_stream_headers(tmp_path):
    with TestClient(create_app(tmp_path / "http.sqlite", runner=PassingRunner())) as client:
        run = client.post("/api/runs", json={"task": "test"}).json()
        url = f"/api/runs/{run['run_id']}/events"
        for cursor in ["", "-1", "1.5", "abc", "9007199254740992", "1" * 5000]:
            response = client.get(url, headers={"Last-Event-ID": cursor})
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "invalid_cursor"
        assert client.get(url, params={"cursor": run["latest_seq"] + 1}).status_code == 409
        assert client.get("/api/runs/missing/events").status_code == 404
        client.app.state.events.replay_window = 3
        expired = client.get(url, headers={"Last-Event-ID": "0"})
        assert expired.status_code == 410
        assert expired.json()["error"] == {
            "code": "cursor_expired",
            "message": "cursor is outside the replay window; fetch a fresh snapshot",
            "earliest_seq": run["latest_seq"] - 2, "latest_seq": run["latest_seq"],
        }


@pytest.mark.parametrize(
    "corrupt_payload", ["{", sqlite3.Binary(b"\x80")],
    ids=["malformed-json", "invalid-utf8-blob"],
)
def test_corrupt_event_payload_fails_initial_replay_explicitly(tmp_path, corrupt_payload):
    with TestClient(
        create_app(tmp_path / "corrupt.sqlite", runner=PassingRunner()),
        raise_server_exceptions=False,
    ) as client:
        run = client.post("/api/runs", json={"task": "corrupt replay"}).json()
        database = client.app.state.coordinator.database
        with database.transaction() as connection:
            event_seq = connection.execute(
                "SELECT MIN(seq) FROM run_events WHERE run_id = ?", (run["run_id"],)
            ).fetchone()[0]
            connection.execute(
                "UPDATE run_events SET payload_json = ? WHERE run_id = ? AND seq = ?",
                (corrupt_payload, run["run_id"], event_seq),
            )

        response = client.get(f"/api/runs/{run['run_id']}/events", params={"cursor": 0})

        assert response.status_code == 409
        assert response.json() == {
            "error": {
                "code": "event_store_corrupt",
                "message": "stored run event payload is invalid",
                "event_seq": event_seq,
            }
        }


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, "condition timed out"
        time.sleep(0.01)


@contextmanager
def running_server(database_path):
    app = create_app(database_path, runner=PassingRunner())
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        wait_until(lambda: server.started)
        app.state.events.page_size = 2
        app.state.events.poll_seconds = 0.01
        app.state.events.heartbeat_seconds = 0.05
        with httpx.Client(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", timeout=3) as client:
            yield client, app
    finally:
        server.should_exit = True
        thread.join(5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(5)
        sock.close()
        assert not thread.is_alive(), "SSE prevented server shutdown"


@pytest.fixture
def live_server(tmp_path):
    with running_server(tmp_path / "live.sqlite") as server:
        yield server


def next_event(lines):
    frame = {}
    for line in lines:
        if not line:
            if "data" in frame:
                assert frame["event"] == "run.event"
                event = json.loads(frame["data"])
                assert int(frame["id"]) == event["seq"]
                return event
            frame = {}
        elif ": " in line:
            key, value = line.split(": ", 1)
            frame[key] = value
    raise AssertionError("stream ended before event")


def test_real_http_disconnect_at_three_replay_to_live_and_cleanup(live_server, monkeypatch):
    client, app = live_server
    run = client.post("/api/runs", json={"task": "test stream"}).json()
    run_id = run["run_id"]
    url = f"/api/runs/{run_id}/events"
    stream = app.state.events
    with client.stream("GET", url) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        lines = response.iter_lines()
        first = [next_event(lines) for _ in range(3)]
        assert [e["seq"] for e in first] == [1, 2, 3]
    wait_until(lambda: stream.active_connections == 0)

    # Commit exactly after an empty replay read: the next poll must see it.
    original = stream.read_page
    injected = False
    reads = []

    def read_page(*args):
        nonlocal injected
        page = original(*args)
        reads.append(page.cursor)
        if not page.events and not injected:
            injected = True
            app.state.coordinator.database.append_node_event(run_id, "review", "node.started", 1)
        return page

    monkeypatch.setattr(stream, "read_page", read_page)
    with client.stream("GET", url + "?cursor=0", headers={"Last-Event-ID": "3"}) as response:
        lines = response.iter_lines()
        remaining = [next_event(lines) for _ in range(run["latest_seq"] - 3 + 1)]
        assert [e["seq"] for e in remaining] == list(range(4, run["latest_seq"] + 2))
        assert injected
        assert next(line for line in lines if line.startswith(": heartbeat")) == ": heartbeat"
    wait_until(lambda: stream.active_connections == 0)
    count = len(reads)
    time.sleep(0.1)
    assert len(reads) == count  # no leaked per-connection poller
    assert app.state.coordinator.database.snapshot(run_id)["latest_seq"] == run["latest_seq"] + 1


def test_real_http_live_decision_and_duplicate_decision(live_server):
    client, app = live_server
    run = client.post("/api/runs", json={"task": "approve while streaming"}).json()
    url = f"/api/runs/{run['run_id']}"
    with client.stream("GET", url + f"/events?cursor={run['latest_seq']}") as response:
        decision = {"decision_id": "once", "patch_revision": 1}
        approved = client.post(url + "/approve", json=decision).json()
        assert approved["status"] == "COMPLETE"
        lines = response.iter_lines()
        events = [next_event(lines) for _ in range(approved["latest_seq"] - run["latest_seq"])]
        assert [e["type"] for e in events] == [
            "decision.recorded", "workspace.published", "run.completed",
        ]
        assert client.post(url + "/approve", json=decision).json() == approved
        assert client.get(url).json() == approved
    wait_until(lambda: app.state.events.active_connections == 0)


def test_http_replay_survives_application_restart(tmp_path):
    path = tmp_path / "restart.sqlite"
    with running_server(path) as (client, app):
        run = client.post("/api/runs", json={"task": "restart stream"}).json()
        url = f"/api/runs/{run['run_id']}/events"
        with client.stream("GET", url) as response:
            lines = response.iter_lines()
            assert [next_event(lines)["seq"] for _ in range(3)] == [1, 2, 3]
        wait_until(lambda: app.state.events.active_connections == 0)
    with running_server(path) as (client, app):
        with client.stream("GET", url, headers={"Last-Event-ID": "3"}) as response:
            lines = response.iter_lines()
            assert [next_event(lines)["seq"] for _ in range(run["latest_seq"] - 3)] == list(
                range(4, run["latest_seq"] + 1),
            )
        wait_until(lambda: app.state.events.active_connections == 0)


def test_javascript_consumer_reconnects_over_real_http(live_server):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for framework-neutral client integration")
    client, app = live_server
    run = client.post("/api/runs", json={"task": "client integration"}).json()
    module = (Path(__file__).resolve().parents[2] / "client" / "run-stream.mjs").as_uri()
    script = """
        import { watchRun, readSSE } from MODULE;
        import assert from "node:assert/strict";
        const run = RUN;
        const stop = new AbortController();
        let wire;
        const seen = [], cursors = [], statuses = [];
        const fetchImpl = async (url, options) => {
            if (!url.endsWith("/events")) return fetch(url, options);
            wire = new AbortController();
            cursors.push(options.headers["Last-Event-ID"]);
            const response = await fetch(url, {...options, signal: AbortSignal.any([options.signal, wire.signal])});
            if (cursors.length !== 1) return response;
            // Fault injection drops both the connection and any prefetched bytes
            // after exactly 3 complete frames, independently of TCP chunking.
            const truncated = new ReadableStream({
                async start(controller) {
                    let count = 0;
                    try {
                        for await (const frame of readSSE(response.body)) {
                            controller.enqueue(new TextEncoder().encode(
                                `id: ${frame.id}\nevent: ${frame.event}\ndata: ${frame.data}\n\n`));
                            if (++count === 3) break;
                        }
                        wire.abort();
                        controller.close();
                    } catch (error) { controller.error(error); }
                },
            });
            return new Response(truncated, {headers: response.headers});
        };
        const state = await watchRun({
            runId: run.run_id, baseUrl: BASE, signal: stop.signal, fetchImpl, retryMs: 1,
            onState(value) { statuses.push(value.status); },
            onEvent(value) {
                seen.push(value.seq);
                if (value.seq === run.latest_seq) stop.abort();
            },
        });
        assert.deepEqual(cursors, ["0", "3"]);
        assert.deepEqual(seen, Array.from({length: run.latest_seq}, (_, i) => i + 1));
        assert.ok(statuses.every(status => status === "AWAITING_APPROVAL"));
        assert.equal(state.snapshot.latest_seq, run.latest_seq);
        console.log(JSON.stringify({cursors, seen, status: state.snapshot.status}));
    """.replace("MODULE", json.dumps(module)).replace("RUN", json.dumps(run)).replace(
        "BASE", json.dumps(str(client.base_url).rstrip("/")),
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["cursors"] == ["0", "3"]
    wait_until(lambda: app.state.events.active_connections == 0)
