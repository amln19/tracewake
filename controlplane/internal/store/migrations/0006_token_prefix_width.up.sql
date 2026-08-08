BEGIN;

-- Token prefixes are `<kind>_<16 hex characters>`. Every kind fitted in 24
-- characters until tenant tokens began carrying the product name: `tracewake_`
-- plus 16 hex is 26. Widening a varchar length is binary-coercible, so this
-- rewrites neither the table nor the unique indexes on these columns.
ALTER TABLE api_tokens ALTER COLUMN prefix TYPE varchar(26);
ALTER TABLE worker_credentials ALTER COLUMN prefix TYPE varchar(26);
ALTER TABLE browser_sessions ALTER COLUMN prefix TYPE varchar(26);

COMMIT;
