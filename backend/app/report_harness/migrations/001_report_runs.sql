CREATE TABLE IF NOT EXISTS report_run_schema_version (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS report_session_evidence (
    session_id text PRIMARY KEY REFERENCES chat_sessions(id) ON DELETE RESTRICT,
    owner_user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    records jsonb NOT NULL DEFAULT '[]'::jsonb
);

CREATE TABLE IF NOT EXISTS report_runs (
    run_id uuid PRIMARY KEY,
    owner_user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    session_id text NOT NULL REFERENCES chat_sessions(id) ON DELETE RESTRICT,
    request_id uuid NOT NULL,
    request_digest text NOT NULL,
    state text NOT NULL CHECK (state IN (
        'queued', 'preparing', 'generating', 'checking', 'revising',
        'published', 'needs_review', 'suspended', 'cancelled', 'failed'
    )),
    state_version bigint NOT NULL DEFAULT 0,
    fencing_token bigint NOT NULL DEFAULT 0,
    lease_owner uuid,
    lease_expires_at timestamptz,
    last_event_seq bigint NOT NULL DEFAULT 0,
    deleted_at timestamptz,
    document jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT report_runs_owner_request UNIQUE (owner_user_id, request_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS report_runs_active_session
    ON report_runs(session_id)
    WHERE state IN ('queued', 'preparing', 'generating', 'checking', 'revising', 'suspended')
      AND deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS report_run_events (
    run_id uuid NOT NULL REFERENCES report_runs(run_id) ON DELETE RESTRICT,
    seq bigint NOT NULL,
    type text NOT NULL CHECK (type IN (
        'stage', 'tool', 'request', 'review', 'budget', 'checkpoint', 'final', 'error'
    )),
    state_version bigint NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    data jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS report_run_requests (
    request_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES report_runs(run_id) ON DELETE RESTRICT,
    attempt_id uuid NOT NULL UNIQUE,
    fencing_token bigint NOT NULL,
    role text NOT NULL,
    endpoint_digest text NOT NULL,
    request_digest text NOT NULL,
    status text NOT NULL CHECK (status IN (
        'intent', 'dispatched', 'committed', 'completion_unknown', 'rejected'
    )),
    reserved_tokens bigint NOT NULL CHECK (reserved_tokens >= 0),
    actual_tokens bigint CHECK (actual_tokens >= 0),
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    settled_at timestamptz
);

INSERT INTO report_run_schema_version(version) VALUES (1)
ON CONFLICT (version) DO NOTHING;
