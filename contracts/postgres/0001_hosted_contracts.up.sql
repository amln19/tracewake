BEGIN;

CREATE TYPE workspace_state AS ENUM ('active', 'disabled', 'deleting');
CREATE TYPE ingestion_state AS ENUM (
    'pending', 'uploaded', 'validating', 'ready', 'invalid', 'deleted'
);
CREATE TYPE job_operation AS ENUM ('diff', 'otlp', 'pprof');
CREATE TYPE job_state AS ENUM (
    'queued', 'running', 'retry_wait', 'succeeded', 'failed', 'cancelled'
);
CREATE TYPE attempt_state AS ENUM (
    'running', 'succeeded', 'failed', 'fenced', 'cancelled'
);
CREATE TYPE artifact_kind AS ENUM (
    'diff_json', 'diff_html', 'otlp_json', 'pprof', 'worker_diagnostic'
);

CREATE TABLE workspaces (
    id uuid PRIMARY KEY,
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200),
    state workspace_state NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    deletion_requested_at timestamptz,
    purge_due_at timestamptz,
    CHECK (
        (state <> 'deleting' AND deletion_requested_at IS NULL AND purge_due_at IS NULL)
        OR
        (state = 'deleting' AND deletion_requested_at IS NOT NULL AND purge_due_at IS NOT NULL)
    )
);

CREATE TABLE api_tokens (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    prefix varchar(24) NOT NULL UNIQUE,
    verifier bytea NOT NULL CHECK (octet_length(verifier) = 32),
    pepper_version smallint NOT NULL CHECK (pepper_version > 0),
    scopes text[] NOT NULL CHECK (cardinality(scopes) BETWEEN 1 AND 16),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    expires_at timestamptz,
    revoked_at timestamptz,
    last_used_at timestamptz,
    CHECK (expires_at IS NULL OR expires_at > created_at)
);
CREATE INDEX api_tokens_workspace_active_idx
    ON api_tokens (workspace_id, created_at DESC)
    WHERE revoked_at IS NULL;

CREATE TABLE worker_credentials (
    id uuid PRIMARY KEY,
    prefix varchar(24) NOT NULL UNIQUE,
    verifier bytea NOT NULL CHECK (octet_length(verifier) = 32),
    pepper_version smallint NOT NULL CHECK (pepper_version > 0),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    expires_at timestamptz,
    revoked_at timestamptz,
    CHECK (expires_at IS NULL OR expires_at > created_at)
);

CREATE TABLE runs (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    state ingestion_state NOT NULL DEFAULT 'pending',
    declared_bundle_format integer NOT NULL CHECK (declared_bundle_format > 0),
    declared_bundle_digest char(64) NOT NULL
        CHECK (declared_bundle_digest ~ '^[0-9a-f]{64}$'),
    declared_bundle_size bigint NOT NULL CHECK (
        declared_bundle_size BETWEEN 0 AND 268435456
    ),
    bundle_object_key text NOT NULL CHECK (char_length(bundle_object_key) BETWEEN 1 AND 512),
    bundle_object_version text,
    validated_bundle_format integer,
    cassette_format_version integer,
    event_schema_version integer,
    logical_run_digest char(64) CHECK (logical_run_digest ~ '^[0-9a-f]{64}$'),
    event_count integer CHECK (event_count BETWEEN 0 AND 100000),
    failure_code varchar(64),
    failure_message varchar(512),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    uploaded_at timestamptz,
    ready_at timestamptz,
    retention_expires_at timestamptz NOT NULL
        DEFAULT (transaction_timestamp() + interval '90 days'),
    deleted_at timestamptz,
    row_version bigint NOT NULL DEFAULT 1 CHECK (row_version > 0),
    UNIQUE (id, workspace_id),
    UNIQUE (workspace_id, declared_bundle_digest),
    UNIQUE (bundle_object_key, bundle_object_version),
    CHECK (
        state <> 'ready'
        OR (
            bundle_object_version IS NOT NULL
            AND validated_bundle_format IS NOT NULL
            AND cassette_format_version IS NOT NULL
            AND event_schema_version IS NOT NULL
            AND logical_run_digest IS NOT NULL
            AND event_count IS NOT NULL
            AND ready_at IS NOT NULL
            AND failure_code IS NULL
        )
    ),
    CHECK (state <> 'invalid' OR failure_code IS NOT NULL),
    CHECK (failure_message IS NULL OR failure_code IS NOT NULL)
);
CREATE INDEX runs_workspace_state_created_idx
    ON runs (workspace_id, state, created_at DESC);
CREATE INDEX runs_retention_idx
    ON runs (retention_expires_at)
    WHERE state <> 'deleted';

CREATE TABLE job_inputs (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    operation job_operation NOT NULL,
    run_a_id uuid NOT NULL,
    run_b_id uuid,
    analysis_profile varchar(64),
    normalized_digest char(64) NOT NULL CHECK (normalized_digest ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    UNIQUE (id, workspace_id),
    UNIQUE (workspace_id, normalized_digest),
    FOREIGN KEY (run_a_id, workspace_id) REFERENCES runs(id, workspace_id),
    FOREIGN KEY (run_b_id, workspace_id) REFERENCES runs(id, workspace_id),
    CHECK (
        (operation = 'diff' AND run_b_id IS NOT NULL AND run_b_id <> run_a_id
            AND analysis_profile = 'align-v1')
        OR
        (operation IN ('otlp', 'pprof') AND run_b_id IS NULL
            AND analysis_profile IS NULL)
    )
);

CREATE TABLE jobs (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    input_id uuid NOT NULL,
    state job_state NOT NULL DEFAULT 'queued',
    current_attempt_number integer,
    max_attempts smallint NOT NULL DEFAULT 3 CHECK (max_attempts = 3),
    retry_at timestamptz,
    cancel_requested_at timestamptz,
    terminal_at timestamptz,
    failure_code varchar(64),
    failure_message varchar(512),
    result_artifact_id uuid,
    result_digest char(64) CHECK (result_digest ~ '^[0-9a-f]{64}$'),
    result_size bigint CHECK (result_size >= 0),
    result_schema_name varchar(64),
    result_schema_version integer CHECK (result_schema_version > 0),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    row_version bigint NOT NULL DEFAULT 1 CHECK (row_version > 0),
    UNIQUE (id, workspace_id),
    FOREIGN KEY (input_id, workspace_id) REFERENCES job_inputs(id, workspace_id),
    CHECK (current_attempt_number IS NULL OR current_attempt_number > 0),
    CHECK ((state = 'retry_wait') = (retry_at IS NOT NULL)),
    CHECK ((state IN ('succeeded', 'failed', 'cancelled')) = (terminal_at IS NOT NULL)),
    CHECK (
        (state = 'succeeded' AND result_artifact_id IS NOT NULL
            AND result_digest IS NOT NULL AND result_size IS NOT NULL
            AND result_schema_name IS NOT NULL AND result_schema_version IS NOT NULL
            AND failure_code IS NULL)
        OR
        (state <> 'succeeded' AND result_artifact_id IS NULL
            AND result_digest IS NULL AND result_size IS NULL
            AND result_schema_name IS NULL AND result_schema_version IS NULL)
    ),
    CHECK (failure_message IS NULL OR failure_code IS NOT NULL)
);
CREATE INDEX jobs_workspace_state_created_idx
    ON jobs (workspace_id, state, created_at DESC);
CREATE INDEX jobs_retry_due_idx
    ON jobs (retry_at, id)
    WHERE state = 'retry_wait';

CREATE TABLE job_attempts (
    job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    attempt_number integer NOT NULL CHECK (attempt_number BETWEEN 1 AND 3),
    worker_id uuid NOT NULL REFERENCES worker_credentials(id),
    state attempt_state NOT NULL DEFAULT 'running',
    token_verifier bytea NOT NULL CHECK (octet_length(token_verifier) = 32),
    token_pepper_version smallint NOT NULL CHECK (token_pepper_version > 0),
    lease_expires_at timestamptz NOT NULL,
    started_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    heartbeat_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    finished_at timestamptz,
    failure_code varchar(64),
    failure_message varchar(512),
    PRIMARY KEY (job_id, attempt_number),
    CHECK ((state = 'running') = (finished_at IS NULL)),
    CHECK (failure_message IS NULL OR failure_code IS NOT NULL)
);
CREATE INDEX job_attempts_expired_idx
    ON job_attempts (lease_expires_at, job_id)
    WHERE state = 'running';

ALTER TABLE jobs
    ADD CONSTRAINT jobs_current_attempt_fk
    FOREIGN KEY (id, current_attempt_number)
    REFERENCES job_attempts(job_id, attempt_number)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE artifacts (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    job_id uuid NOT NULL,
    attempt_number integer NOT NULL,
    kind artifact_kind NOT NULL,
    object_key text NOT NULL CHECK (char_length(object_key) BETWEEN 1 AND 512),
    object_version text NOT NULL CHECK (char_length(object_version) BETWEEN 1 AND 256),
    digest char(64) NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
    size bigint NOT NULL CHECK (size >= 0),
    media_type varchar(128) NOT NULL,
    schema_name varchar(64),
    schema_version integer CHECK (schema_version > 0),
    authoritative boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    retention_expires_at timestamptz NOT NULL
        DEFAULT (transaction_timestamp() + interval '24 hours'),
    FOREIGN KEY (job_id, workspace_id) REFERENCES jobs(id, workspace_id),
    FOREIGN KEY (job_id, attempt_number)
        REFERENCES job_attempts(job_id, attempt_number),
    UNIQUE (object_key, object_version),
    CHECK ((schema_name IS NULL) = (schema_version IS NULL))
);
CREATE UNIQUE INDEX artifacts_one_authoritative_kind_idx
    ON artifacts (job_id, kind)
    WHERE authoritative;
CREATE INDEX artifacts_orphan_retention_idx
    ON artifacts (retention_expires_at, id)
    WHERE NOT authoritative;

ALTER TABLE jobs
    ADD CONSTRAINT jobs_result_artifact_fk
    FOREIGN KEY (result_artifact_id) REFERENCES artifacts(id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE progress_snapshots (
    job_id uuid PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    attempt_number integer NOT NULL,
    sequence bigint NOT NULL CHECK (sequence > 0),
    stage varchar(32) NOT NULL CHECK (
        stage IN ('claiming', 'downloading', 'validating', 'analyzing',
                  'uploading', 'committing')
    ),
    message varchar(512) NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    FOREIGN KEY (job_id, attempt_number)
        REFERENCES job_attempts(job_id, attempt_number)
);

CREATE TABLE idempotency_records (
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    operation varchar(64) NOT NULL,
    idempotency_key varchar(255) NOT NULL,
    request_digest char(64) NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    response_kind varchar(64) NOT NULL,
    response_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    expires_at timestamptz NOT NULL
        DEFAULT (transaction_timestamp() + interval '24 hours'),
    PRIMARY KEY (workspace_id, operation, idempotency_key),
    CHECK (expires_at > created_at)
);
CREATE INDEX idempotency_expiry_idx ON idempotency_records (expires_at);

CREATE TABLE outbox (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    aggregate_type varchar(32) NOT NULL,
    aggregate_id uuid NOT NULL,
    aggregate_version bigint NOT NULL CHECK (aggregate_version > 0),
    topic varchar(64) NOT NULL,
    payload jsonb NOT NULL CHECK (octet_length(payload::text) <= 4096),
    available_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    claimed_at timestamptz,
    claimed_by uuid,
    published_at timestamptz,
    publish_attempts integer NOT NULL DEFAULT 0 CHECK (publish_attempts >= 0),
    last_error_code varchar(64),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    UNIQUE (aggregate_type, aggregate_id, aggregate_version, topic),
    CHECK ((claimed_at IS NULL) = (claimed_by IS NULL))
);
CREATE INDEX outbox_publishable_idx
    ON outbox (available_at, id)
    WHERE published_at IS NULL;

CREATE TABLE audit_records (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id uuid REFERENCES workspaces(id),
    aggregate_type varchar(32) NOT NULL,
    aggregate_id uuid NOT NULL,
    event_type varchar(64) NOT NULL,
    actor_type varchar(32) NOT NULL,
    actor_id uuid,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (octet_length(payload::text) <= 4096),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    retention_expires_at timestamptz NOT NULL
        DEFAULT (transaction_timestamp() + interval '365 days')
);
CREATE INDEX audit_workspace_cursor_idx
    ON audit_records (workspace_id, created_at DESC, id DESC);
CREATE INDEX audit_retention_idx ON audit_records (retention_expires_at);

COMMIT;
