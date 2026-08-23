ALTER TYPE job_operation ADD VALUE IF NOT EXISTS 'localize';

-- tracewake-statement-break

ALTER TYPE artifact_kind ADD VALUE IF NOT EXISTS 'localize_json';

-- tracewake-statement-break

ALTER TYPE artifact_kind ADD VALUE IF NOT EXISTS 'localize_result_json';

-- tracewake-statement-break

BEGIN;

-- localize is the single-trace rule on its own: one run, no alignment, and its
-- own profile. The diff branch is unchanged from 0008 and is restated because
-- a CHECK constraint can only be replaced whole.
ALTER TABLE job_inputs DROP CONSTRAINT job_inputs_shape_check;
ALTER TABLE job_inputs ADD CONSTRAINT job_inputs_shape_check CHECK (
    (operation = 'diff' AND run_b_id IS NOT NULL AND run_b_id <> run_a_id
        AND analysis_profile = 'align-v2')
    OR
    (operation = 'localize' AND run_b_id IS NULL
        AND analysis_profile = 'localize-v1')
    OR
    (operation IN ('otlp', 'pprof', 'validate') AND run_b_id IS NULL
        AND analysis_profile IS NULL)
);

COMMIT;
