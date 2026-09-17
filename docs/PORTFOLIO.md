# DevFlow portfolio notes

This page turns the repository's implementation evidence into honest resume and interview
language. It intentionally describes only behavior supported by the current source and tests.

## One-line project description

Built a local-first code-change approval workflow that persists orchestration state,
replays realtime events after disconnects, validates generated candidates in constrained
Docker containers, and atomically publishes approved workspace revisions.

## Resume bullets

Choose three or four bullets that fit the role and the space available:

- Built a human-in-the-loop approval workflow with FastAPI, LangGraph checkpoints, and
  SQLite, persisting run state, patches, check/review artifacts, decisions, and domain
  events across process restarts.
- Designed a durable SSE protocol with monotonic sequence numbers, `Last-Event-ID` replay,
  bounded history, expired-cursor recovery, UTF-8 chunk handling, and exponential-backoff
  reconnects while keeping REST snapshots authoritative.
- Implemented an integer-revision managed workspace with system-derived candidate paths,
  link/path traversal defenses, staging validation, and recoverable atomic publication so
  candidate execution never mutates the published revision.
- Added idempotent approval decisions and renewable resume leases, with independent-process
  tests covering duplicate requests, interrupted checkpoints, lease loss, and publication
  recovery windows.
- Isolated candidate lint/test execution in constrained Docker containers using read-only
  inputs and root filesystem, no network, dropped capabilities, non-root execution, and
  CPU, memory, PID, time, and output limits; kept Docker-socket access in a separate local
  dispatcher rather than the API container.
- Delivered a Next.js workbench with persisted event timeline, approval controls, and a
  Monaco diff viewer with worker and raw-text fallbacks; covered client behavior with Node
  unit tests and Playwright acceptance tests.

## Short project entry

**DevFlow — Durable code-change approval workflow**

Python, FastAPI, LangGraph, SQLite, Next.js, React, Docker, Playwright

Engineered a reproducible local workflow that turns a task into a deterministic candidate
patch, runs isolated checks and review, pauses for a persisted human decision, and publishes
an approved workspace revision. Focused on restart recovery, idempotency, event replay, and
explicit trust boundaries rather than claiming production AI autonomy.

## Interview walkthrough

1. Start with scope: one trusted local user, one API worker, SQLite, and a deterministic
   mock provider. The project demonstrates workflow infrastructure, not model quality.
2. Trace one run through plan, code, candidate materialization, lint/test, review, approval,
   and publication. Show that the candidate and published workspace are distinct.
3. Disconnect and reload the workbench. Explain how persisted event sequence numbers and
   REST reconciliation prevent stale snapshots and missed timeline entries.
4. Approve a run, then point to the decision idempotency key, renewable resume lease,
   checkpoint, publication record, and atomic rename used for recovery.
5. Explain the runner boundary: fixed actions, constrained containers, and a Compose-only
   local dispatcher that holds Docker access. State clearly that this is defense in depth,
   not a proof against kernel or container escape.
6. Close with tradeoffs: no real LLM provider, revise loop, authentication, multi-user
   isolation, background queue, multi-worker writes, or high availability yet.

## Evidence and wording guardrails

Repository evidence lives in `README.md` under **Evidence map**. Before quoting test or CI
results, use results from the exact commit being presented. A checked-in GitHub Actions
workflow is not evidence of a successful hosted run.

Do not describe this project as a multi-agent system, a real-LLM coding agent, a complete
security sandbox, a production-ready review platform, or a high-availability service.
Prefer: durable approval workflow, deterministic provider, constrained container execution,
persisted SSE replay, versioned workspace, and recoverable publication.

## GitHub repository description and topics

Suggested About description:

> Durable human-in-the-loop code-change workflow with persisted SSE replay, constrained Docker checks, and recoverable workspace publication.

Suggested topics:

`fastapi`, `langgraph`, `nextjs`, `sqlite`, `docker`, `sse`, `human-in-the-loop`,
`workflow-engine`, `playwright`, `reliability-engineering`
