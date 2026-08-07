BEGIN;

CREATE TABLE browser_sessions (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    prefix varchar(24) NOT NULL UNIQUE,
    verifier bytea NOT NULL CHECK (octet_length(verifier) = 32),
    csrf_verifier bytea NOT NULL CHECK (octet_length(csrf_verifier) = 32),
    pepper_version smallint NOT NULL CHECK (pepper_version > 0),
    scopes text[] NOT NULL CHECK (cardinality(scopes) BETWEEN 1 AND 16),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    last_used_at timestamptz,
    CHECK (expires_at > created_at)
);

CREATE INDEX browser_sessions_workspace_active_idx
    ON browser_sessions (workspace_id, expires_at)
    WHERE revoked_at IS NULL;
CREATE INDEX browser_sessions_expiry_idx
    ON browser_sessions (expires_at)
    WHERE revoked_at IS NULL;

COMMIT;
