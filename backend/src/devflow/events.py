"""Durable, per-run SSE replay and live tailing (single-worker local MVP)."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import closing
from dataclasses import dataclass
from time import monotonic

from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from devflow.database import Database


class CursorError(Exception):
    def __init__(self, code: str, message: str, status: int, **details):
        super().__init__(message)
        self.status = status
        self.body = {"error": {"code": code, "message": message, **details}}


class EventStoreError(Exception):
    # The affected durable resource cannot currently satisfy replay. A 4xx
    # response keeps generic clients from treating the persistent row as a
    # transient server outage and reconnecting forever.
    status = 409

    def __init__(self, event_seq: int):
        super().__init__("stored run event payload is invalid")
        self.body = {
            "error": {
                "code": "event_store_corrupt",
                "message": str(self),
                "event_seq": event_seq,
            }
        }


def parse_cursor(value: str | None) -> int | None:
    if value is None:
        return None
    # Also safe to represent exactly in the browser's Number type.
    if not re.fullmatch(r"[0-9]{1,16}", value) or int(value) > 2**53 - 1:
        raise CursorError("invalid_cursor", "cursor must be a non-negative safe integer", 400)
    return int(value)


@dataclass
class EventPage:
    cursor: int
    events: list[dict]


class EventStream:
    def __init__(
        self, database: Database, *, replay_window: int = 1000, page_size: int = 100,
        poll_seconds: float = 0.25, heartbeat_seconds: float = 15,
    ):
        if min(replay_window, page_size, poll_seconds, heartbeat_seconds) <= 0:
            raise ValueError("stream limits and intervals must be positive")
        self.database = database
        self.replay_window = replay_window
        self.page_size = min(page_size, replay_window)
        self.poll_seconds = poll_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.active_connections = 0

    def read_page(self, run_id: str, cursor: int | None) -> EventPage:
        # No connection/transaction is held across yields, sleeps or network IO.
        with closing(self.database.connect()) as connection:
            connection.execute("BEGIN")
            if connection.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is None:
                raise CursorError("run_not_found", "run not found", 404)
            latest = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM run_events WHERE run_id = ?", (run_id,),
            ).fetchone()[0]
            # A serving window, NOT physical deletion. Keeping the durable log
            # preserves MAX(seq) allocation without changing the existing schema.
            floor = max(0, latest - self.replay_window)
            after = floor if cursor is None else cursor
            if after < floor:
                raise CursorError(
                    "cursor_expired", "cursor is outside the replay window; fetch a fresh snapshot",
                    410, earliest_seq=floor + 1, latest_seq=latest,
                )
            if after > latest:
                raise CursorError("cursor_ahead", "cursor is ahead of this run", 409, latest_seq=latest)
            rows = connection.execute(
                "SELECT * FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (run_id, after, self.page_size),
            ).fetchall()
            events = []
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"])
                except (TypeError, ValueError, RecursionError) as error:
                    raise EventStoreError(row["seq"]) from error
                events.append({
                    "run_id": row["run_id"], "seq": row["seq"], "type": row["type"],
                    "node": row["node"], "payload": payload, "created_at": row["created_at"],
                })
            return EventPage(after, events)

    async def frames(self, run_id: str, page: EventPage):
        self.active_connections += 1
        cursor = page.cursor
        last_sent = monotonic()
        try:
            yield "retry: 1000\n\n"
            while True:
                for event in page.events:
                    # Use a fixed valid SSE event name. The business type stays
                    # in JSON, so a stored label cannot inject SSE fields.
                    yield f"id: {event['seq']}\nevent: run.event\ndata: {json.dumps(event)}\n\n"
                    cursor = event["seq"]
                    last_sent = monotonic()
                if not page.events:
                    if monotonic() - last_sent >= self.heartbeat_seconds:
                        yield ": heartbeat\n\n"
                        last_sent = monotonic()
                    await asyncio.sleep(min(self.poll_seconds, self.heartbeat_seconds))
                try:
                    # Replay and live use the same durable > cursor query; there
                    # is no separate subscribe operation or lossy handoff window.
                    page = await run_in_threadpool(self.read_page, run_id, cursor)
                except CursorError as error:
                    # HTTP headers are already sent. No id: control frames must
                    # not advance the last successfully applied business cursor.
                    yield f"event: stream.reset\ndata: {json.dumps(error.body)}\n\n"
                    return
                except EventStoreError as error:
                    # A durable bad row cannot be repaired by reconnecting from
                    # the same cursor. Terminate explicitly without advancing it.
                    yield f"event: stream.error\ndata: {json.dumps(error.body)}\n\n"
                    return
        finally:
            self.active_connections -= 1


class EventStreamResponse(StreamingResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Also close on ASGI 2.4 send failures, outside the generator itself.
            await self.body_iterator.aclose()
