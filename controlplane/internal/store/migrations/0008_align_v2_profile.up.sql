BEGIN;

-- The alignment is unchanged, but its divergence field now reports the
-- single-trace rule rather than the last aligned column that agreed. A stored
-- diff names the profile that produced it, so the changed meaning takes a
-- changed name with it.
ALTER TABLE job_inputs DROP CONSTRAINT job_inputs_shape_check;
ALTER TABLE job_inputs ADD CONSTRAINT job_inputs_shape_check CHECK (
    (operation = 'diff' AND run_b_id IS NOT NULL AND run_b_id <> run_a_id
        AND analysis_profile = 'align-v2')
    OR
    (operation IN ('otlp', 'pprof', 'validate') AND run_b_id IS NULL
        AND analysis_profile IS NULL)
);

COMMIT;
