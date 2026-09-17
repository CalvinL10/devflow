PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    current_revision INTEGER NOT NULL DEFAULT 0 CHECK (current_revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    singleton_key INTEGER NOT NULL DEFAULT 1 CHECK (singleton_key = 1),
    thread_id TEXT NOT NULL UNIQUE,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    base_workspace_revision INTEGER NOT NULL CHECK (base_workspace_revision >= 0),
    status TEXT NOT NULL CHECK (status IN (
        'CREATED', 'RUNNING', 'AWAITING_APPROVAL', 'APPLYING',
        'COMPLETE', 'REJECTED', 'CANCELLED', 'FAILED'
    )),
    apply_started_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_runs_single_active
ON runs(singleton_key)
WHERE status IN ('CREATED', 'RUNNING', 'AWAITING_APPROVAL', 'APPLYING');

CREATE TABLE IF NOT EXISTS patches (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    patch_revision INTEGER NOT NULL CHECK (patch_revision >= 1),
    base_workspace_revision INTEGER NOT NULL CHECK (base_workspace_revision >= 0),
    candidate_dir TEXT NOT NULL,
    patch_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, patch_revision),
    UNIQUE (id, run_id, patch_revision)
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    patch_id TEXT NOT NULL,
    patch_revision INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('approve', 'reject')),
    feedback TEXT,
    result_status TEXT NOT NULL,
    resume_completed_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (patch_id, run_id, patch_revision)
        REFERENCES patches(id, run_id, patch_revision)
);

CREATE TABLE IF NOT EXISTS decision_resume_claims (
    decision_id TEXT PRIMARY KEY REFERENCES decisions(decision_id) ON DELETE CASCADE,
    owner_id TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_events (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL CHECK (seq >= 1),
    type TEXT NOT NULL,
    node TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS run_checkpoint_refs (
    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    thread_id TEXT NOT NULL REFERENCES runs(thread_id),
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_artifacts (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('task', 'plan', 'check_report', 'review_report')),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, kind)
);
