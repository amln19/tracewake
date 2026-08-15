ALTER TYPE job_operation ADD VALUE IF NOT EXISTS 'validate';

-- tracewake-statement-break

BEGIN;

ALTER TABLE job_inputs DROP CONSTRAINT job_inputs_check;
ALTER TABLE job_inputs ADD CONSTRAINT job_inputs_shape_check CHECK (
    (operation = 'diff' AND run_b_id IS NOT NULL AND run_b_id <> run_a_id
        AND analysis_profile = 'align-v1')
    OR
    (operation IN ('otlp', 'pprof', 'validate') AND run_b_id IS NULL
        AND analysis_profile IS NULL)
);

COMMIT;
