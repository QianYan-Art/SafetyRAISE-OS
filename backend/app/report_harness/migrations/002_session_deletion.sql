-- 解除会话物理删除与报告账本之间的外键耦合；账本仍由自身外键保护。
ALTER TABLE IF EXISTS report_session_evidence
    DROP CONSTRAINT IF EXISTS report_session_evidence_session_id_fkey;

ALTER TABLE IF EXISTS report_runs
    DROP CONSTRAINT IF EXISTS report_runs_session_id_fkey;

CREATE TABLE IF NOT EXISTS session_deletion_barriers (
    session_id text PRIMARY KEY,
    deleted_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO report_run_schema_version(version) VALUES (2)
ON CONFLICT (version) DO NOTHING;
