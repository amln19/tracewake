export type Failure = {
  schema_version: number;
  code: string;
  message: string;
  retryable: boolean;
};

export type Run = {
  run_id: string;
  state: "pending" | "uploaded" | "validating" | "ready" | "invalid" | "deleted";
  bundle_digest: string;
  bundle_format_version: number;
  logical_run_digest: string | null;
  cassette_format_version: number | null;
  event_schema_version: number | null;
  event_count: number | null;
  failure: Failure | null;
  created_at: string;
  retention_expires_at: string;
  ready_at: string | null;
};

export type Attempt = {
  attempt_number: number;
  state: "running" | "succeeded" | "failed" | "fenced" | "cancelled";
  started_at: string;
  finished_at: string | null;
  failure: Failure | null;
};

export type Progress = {
  protocol_version: number;
  attempt_number: number;
  sequence: number;
  stage: string;
  message: string;
};

export type Artifact = {
  artifact_id: string;
  kind: string;
  digest: string;
  size: number;
  media_type: string;
  schema_name: string | null;
  schema_version: number | null;
  retention_expires_at: string;
};

export type Job = {
  job_id: string;
  operation: "diff" | "otlp" | "pprof";
  state: "queued" | "running" | "retry_wait" | "succeeded" | "failed" | "cancelled";
  run_ids: string[];
  profile: string | null;
  current_attempt_number: number | null;
  attempts: Attempt[];
  progress: Progress | null;
  cancel_requested_at: string | null;
  failure: Failure | null;
  created_at: string;
  updated_at: string;
  terminal_at: string | null;
  artifacts: Artifact[];
};

export type AuditRecord = {
  id: number;
  aggregate_type: string;
  aggregate_id: string;
  event_type: string;
  actor_type: string;
  created_at: string;
};

export type Session = {
  csrf_token: string;
  expires_at: string;
  scopes: string[];
};

export type UploadGrant = {
  upload_id: string;
  run_id: string;
  required_digest: string;
  required_size: number;
  upload_url: string;
  upload_headers: Record<string, string>;
  expires_at: string;
};
