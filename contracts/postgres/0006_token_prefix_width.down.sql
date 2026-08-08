BEGIN;

-- This fails while any retained prefix is wider than 24 characters, which is
-- every tenant token issued since the widening. Narrowing is available only on
-- an environment that has issued none; anywhere else, rollback requires an
-- operator-approved restore.
ALTER TABLE api_tokens ALTER COLUMN prefix TYPE varchar(24);
ALTER TABLE worker_credentials ALTER COLUMN prefix TYPE varchar(24);
ALTER TABLE browser_sessions ALTER COLUMN prefix TYPE varchar(24);

COMMIT;
