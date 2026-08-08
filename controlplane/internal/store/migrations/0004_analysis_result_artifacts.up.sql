ALTER TYPE artifact_kind ADD VALUE IF NOT EXISTS 'otlp_result_json';

-- tracewake-statement-break

ALTER TYPE artifact_kind ADD VALUE IF NOT EXISTS 'pprof_result_json';
