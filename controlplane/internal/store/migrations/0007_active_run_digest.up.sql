BEGIN;

ALTER TABLE runs DROP CONSTRAINT runs_workspace_id_declared_bundle_digest_key;
CREATE UNIQUE INDEX runs_workspace_active_digest_idx
    ON runs (workspace_id, declared_bundle_digest)
    WHERE state <> 'deleted';

COMMIT;
