BEGIN;

DROP TABLE audit_records;
DROP TABLE outbox;
DROP TABLE idempotency_records;
DROP TABLE progress_snapshots;
ALTER TABLE jobs DROP CONSTRAINT jobs_result_artifact_fk;
DROP TABLE artifacts;
ALTER TABLE jobs DROP CONSTRAINT jobs_current_attempt_fk;
DROP TABLE job_attempts;
DROP TABLE jobs;
DROP TABLE job_inputs;
DROP TABLE runs;
DROP TABLE worker_credentials;
DROP TABLE api_tokens;
DROP TABLE workspaces;

DROP TYPE artifact_kind;
DROP TYPE attempt_state;
DROP TYPE job_state;
DROP TYPE job_operation;
DROP TYPE ingestion_state;
DROP TYPE workspace_state;

COMMIT;
