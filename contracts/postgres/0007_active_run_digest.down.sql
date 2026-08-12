BEGIN;

-- This fails while any workspace holds more than one run sharing a bundle
-- digest, which is every workspace that uploaded a bundle again after deleting
-- its earlier run -- precisely what the partial index exists to allow. The
-- table-wide constraint is restorable only on an environment that has reused no
-- digest; anywhere else, rollback requires an operator-approved restore.
DROP INDEX runs_workspace_active_digest_idx;
ALTER TABLE runs ADD CONSTRAINT runs_workspace_id_declared_bundle_digest_key
    UNIQUE (workspace_id, declared_bundle_digest);

COMMIT;
