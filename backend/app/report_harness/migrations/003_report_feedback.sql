-- 报告质量反馈：每次保存追加一个修订，不改写旧意见；最新修订即当前反馈。
-- run_context 冻结保存时的运行状态与版本摘要，会话或运行之后被删除也能追溯。
CREATE TABLE IF NOT EXISTS report_run_feedback (
    run_id uuid NOT NULL REFERENCES report_runs(run_id) ON DELETE RESTRICT,
    revision integer NOT NULL CHECK (revision >= 1),
    author_user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    reviewer_name text NOT NULL,
    verdict text NOT NULL CHECK (verdict IN ('usable', 'needs_revision', 'unusable')),
    issue_tags text[] NOT NULL DEFAULT '{}',
    comment text NOT NULL DEFAULT '',
    run_context jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, revision)
);

CREATE INDEX IF NOT EXISTS report_run_feedback_created_at
    ON report_run_feedback(created_at DESC);

INSERT INTO report_run_schema_version(version) VALUES (3)
ON CONFLICT (version) DO NOTHING;
