import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import {
  APIError,
  artifactURL,
  cancelJob,
  createDiff,
  endSession,
  exchangeToken,
  getJob,
  listAudit,
  listRuns,
  readResult,
  restoreSession,
  subscribeToJob,
  uploadBundle,
} from "./api";
import type { Artifact, AuditRecord, Job, Run, Session } from "./types";

type Route = { page: "overview" } | { page: "job"; id: string };

function routeFromLocation(): Route {
  const match = window.location.pathname.match(/^\/jobs\/([0-9a-f-]+)$/i);
  return match ? { page: "job", id: match[1] } : { page: "overview" };
}

function navigate(pathname: string): void {
  window.history.pushState({}, "", pathname);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

function short(value: string | null | undefined, size = 10): string {
  return value ? `${value.slice(0, size)}…` : "—";
}

function date(value: string | null): string {
  if (!value) return "—";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / 1024 / 1024).toFixed(1)} MiB`;
}

function errorText(error: unknown): string {
  if (error instanceof APIError || error instanceof Error) return error.message;
  return "The request could not be completed.";
}

export function App() {
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  const [route, setRoute] = useState<Route>(routeFromLocation);

  useEffect(() => {
    const change = () => setRoute(routeFromLocation());
    window.addEventListener("popstate", change);
    return () => window.removeEventListener("popstate", change);
  }, []);

  useEffect(() => {
    void restoreSession().then(setSession).catch(() => setSession(null));
  }, []);

  if (session === undefined) return <LoadingScreen />;
  if (session === null) return <SessionExchange onSession={setSession} />;
  return (
    <Shell
      session={session}
      route={route}
      onExpired={() => setSession(null)}
      onSignOut={async () => {
        await endSession();
        navigate("/");
        setSession(null);
      }}
    />
  );
}

function LoadingScreen() {
  return (
    <main className="centered" aria-live="polite">
      <div className="brand-mark">T</div>
      <p className="eyebrow">Tracewake control room</p>
      <h1>Reconstructing authoritative state</h1>
      <div className="loading-line" />
    </main>
  );
}

function SessionExchange({ onSession }: { onSession: (session: Session) => void }) {
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const durableToken = token;
    setToken("");
    event.currentTarget.reset();
    try {
      onSession(await exchangeToken(durableToken));
    } catch (cause) {
      setError(errorText(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="exchange-grid">
      <section className="exchange-story">
        <div className="brand-lockup"><span className="brand-mark">T</span><span>Tracewake</span></div>
        <p className="eyebrow">Recorded execution, made inspectable</p>
        <h1>Find the precise turn where trajectories part ways.</h1>
        <p className="lede">Upload immutable run bundles, follow validation and retries, and inspect results without moving lifecycle authority into the browser.</p>
        <div className="signal-strip" aria-label="System properties">
          <span>Local-first</span><span>Attempt-fenced</span><span>Byte-verifiable</span>
        </div>
      </section>
      <section className="exchange-card" aria-labelledby="exchange-title">
        <p className="eyebrow">Secure session exchange</p>
        <h2 id="exchange-title">Enter a workspace token</h2>
        <p>The token is exchanged once for a 15-minute HttpOnly session. It is cleared from this form and never placed in browser storage.</p>
        <form onSubmit={submit}>
          <label htmlFor="workspace-token">Workspace token</label>
          <input id="workspace-token" name="token" type="password" autoComplete="off" required value={token} onChange={(event) => setToken(event.target.value)} />
          <button className="primary" disabled={busy}>{busy ? "Exchanging…" : "Open control room"}</button>
        </form>
        {error && <p className="error" role="alert">{error}</p>}
        <div className="security-note"><span className="status-dot ready" />Secure cookie · strict same-site · CSRF bound</div>
      </section>
    </main>
  );
}

function Shell({ session, route, onExpired, onSignOut }: { session: Session; route: Route; onExpired: () => void; onSignOut: () => Promise<void> }) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [audit, setAudit] = useState<AuditRecord[]>([]);
  const [error, setError] = useState("");
  const reload = useCallback(async () => {
    try {
      const [nextRuns, nextAudit] = await Promise.all([listRuns(), listAudit()]);
      setRuns(nextRuns);
      setAudit(nextAudit);
      setError("");
    } catch (cause) {
      if (cause instanceof APIError && cause.status === 401) onExpired();
      else setError(errorText(cause));
    }
  }, [onExpired]);

  useEffect(() => { void reload(); }, [reload]);
  useEffect(() => {
    if (!runs.some((run) => ["pending", "uploaded", "validating"].includes(run.state))) return;
    const timer = window.setInterval(() => void reload(), 2000);
    return () => window.clearInterval(timer);
  }, [reload, runs]);

  return (
    <div className="app-shell">
      <header className="topbar">
        <button className="wordmark" onClick={() => navigate("/")} aria-label="Tracewake overview"><span className="brand-mark">T</span><span>Tracewake</span></button>
        <div className="system-state"><span className="status-dot ready" />API state is authoritative</div>
        <div className="top-actions"><span className="session-time">Session until {date(session.expires_at)}</span><button className="quiet" onClick={() => void onSignOut()}>Sign out</button></div>
      </header>
      <nav className="rail" aria-label="Primary">
        <button className={route.page === "overview" ? "active" : ""} onClick={() => navigate("/")}><span>01</span>Runs</button>
        <a href="#audit"><span>02</span>Audit</a>
        {route.page === "job" && <button className="active"><span>03</span>Analysis</button>}
      </nav>
      <main className="workspace">
        {error && <div className="error banner" role="alert">{error}<button onClick={() => void reload()}>Retry</button></div>}
        {route.page === "overview" ? <Overview runs={runs} audit={audit} onChanged={reload} /> : <JobDetail jobID={route.id} onExpired={onExpired} />}
      </main>
    </div>
  );
}

function Overview({ runs, audit, onChanged }: { runs: Run[]; audit: AuditRecord[]; onChanged: () => Promise<void> }) {
  const readyRuns = runs.filter((run) => run.state === "ready");
  const [selected, setSelected] = useState<string[]>([]);
  const [uploadState, setUploadState] = useState("");
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);

  function toggle(runID: string) {
    setSelected((current) => current.includes(runID) ? current.filter((id) => id !== runID) : [...current.slice(-1), runID]);
  }

  async function upload(file: File | undefined) {
    if (!file) return;
    setError("");
    try {
      const run = await uploadBundle(file, setUploadState);
      setUploadState(`Validation queued for ${short(run.run_id)}`);
      await onChanged();
    } catch (cause) {
      setError(errorText(cause));
      setUploadState("");
    }
  }

  async function submitDiff() {
    if (selected.length !== 2) return;
    setCreating(true);
    setError("");
    try {
      const jobID = await createDiff([selected[0], selected[1]]);
      navigate(`/jobs/${jobID}`);
    } catch (cause) {
      setError(errorText(cause));
    } finally {
      setCreating(false);
    }
  }

  return (
    <>
      <section className="hero-row">
        <div><p className="eyebrow">Run inventory</p><h1>Choose two trajectories.<br /><em>Locate the split.</em></h1></div>
        <label className="upload-button">Upload bundle<input type="file" accept=".tar,application/x-tar" onChange={(event) => { void upload(event.target.files?.[0]); event.target.value = ""; }} /></label>
      </section>
      {(uploadState || error) && <div className={error ? "error banner" : "notice banner"} role="status">{error || uploadState}</div>}
      <section className="metrics" aria-label="Run summary">
        <Metric value={runs.length} label="Recorded runs" />
        <Metric value={readyRuns.length} label="Ready to analyze" />
        <Metric value={runs.filter((run) => run.state === "validating").length} label="Validating now" />
        <Metric value={audit.length} label="Recent audit facts" />
      </section>
      <section className="panel run-panel">
        <div className="panel-heading"><div><p className="eyebrow">Immutable inputs</p><h2>Runs</h2></div><div className="selection-action"><span>{selected.length}/2 selected</span><button className="primary compact" disabled={selected.length !== 2 || creating} onClick={() => void submitDiff()}>{creating ? "Creating…" : "Create lexical diff"}</button></div></div>
        <div className="run-table" role="table" aria-label="Workspace runs">
          <div className="table-row table-head" role="row"><span>Compare</span><span>Run</span><span>State</span><span>Events</span><span>Logical digest</span><span>Created</span></div>
          {runs.map((run) => <RunRow key={run.run_id} run={run} selected={selected.includes(run.run_id)} onToggle={() => toggle(run.run_id)} />)}
          {runs.length === 0 && <div className="empty-state">No runs yet. Upload a deterministic bundle to begin.</div>}
        </div>
      </section>
      <AuditTimeline records={audit} />
    </>
  );
}

function Metric({ value, label }: { value: number; label: string }) {
  return <div className="metric"><strong>{String(value).padStart(2, "0")}</strong><span>{label}</span></div>;
}

function RunRow({ run, selected, onToggle }: { run: Run; selected: boolean; onToggle: () => void }) {
  return (
    <div className={`table-row ${selected ? "selected" : ""}`} role="row">
      <span><input type="checkbox" aria-label={`Compare run ${run.run_id}`} checked={selected} disabled={run.state !== "ready"} onChange={onToggle} /></span>
      <span className="mono" title={run.run_id}>{short(run.run_id, 8)}</span>
      <span><StateBadge state={run.state} /></span>
      <span>{run.event_count ?? "—"}</span>
      <span className="mono" title={run.logical_run_digest ?? ""}>{short(run.logical_run_digest, 12)}</span>
      <span>{date(run.created_at)}</span>
      {run.failure && <span className="row-failure">{run.failure.code}</span>}
    </div>
  );
}

function AuditTimeline({ records }: { records: AuditRecord[] }) {
  return (
    <section className="panel audit-panel" id="audit">
      <div className="panel-heading"><div><p className="eyebrow">Bounded history</p><h2>Audit timeline</h2></div><span className="muted">Latest 100 meaningful transitions</span></div>
      <ol className="timeline">
        {records.slice(0, 12).map((record) => (
          <li key={record.id}><span className="timeline-index">{String(record.id).padStart(3, "0")}</span><div><strong>{record.event_type.replaceAll("_", " ")}</strong><span>{record.aggregate_type} · {short(record.aggregate_id)}</span></div><time>{date(record.created_at)}</time></li>
        ))}
        {records.length === 0 && <li className="empty-state">No lifecycle transitions have been recorded.</li>}
      </ol>
    </section>
  );
}

function JobDetail({ jobID, onExpired }: { jobID: string; onExpired: () => void }) {
  const [job, setJob] = useState<Job | null>(null);
  const [result, setResult] = useState<unknown>(null);
  const [error, setError] = useState("");
  const [report, setReport] = useState(false);
  const terminal = job ? ["succeeded", "failed", "cancelled"].includes(job.state) : false;
  const load = useCallback(async () => {
    try {
      const next = await getJob(jobID);
      setJob(next);
      setError("");
      const resultArtifact = next.artifacts.find((artifact) => artifact.schema_name === "result-envelope");
      if (resultArtifact) setResult(await readResult(resultArtifact.artifact_id));
    } catch (cause) {
      if (cause instanceof APIError && cause.status === 401) onExpired();
      else setError(errorText(cause));
    }
  }, [jobID, onExpired]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (terminal) return;
    const close = subscribeToJob(jobID, () => void load());
    const timer = window.setInterval(() => void load(), 2000);
    return () => { close(); window.clearInterval(timer); };
  }, [jobID, load, terminal]);

  const provenance = useMemo(() => {
    if (!result || typeof result !== "object") return null;
    const value = result as Record<string, unknown>;
    return value.provenance && typeof value.provenance === "object" ? value.provenance as Record<string, unknown> : null;
  }, [result]);

  if (!job) return <section className="panel loading-panel"><p className="eyebrow">Analysis</p><h1>{error || "Loading authoritative job state…"}</h1></section>;
  const html = job.artifacts.find((artifact) => artifact.kind === "diff_html");
  return (
    <>
      <section className="job-hero">
        <div><button className="back-link" onClick={() => navigate("/")}>← All runs</button><p className="eyebrow">{job.operation} analysis · {short(job.job_id)}</p><h1>{job.operation === "diff" ? "Trajectory divergence" : `${job.operation.toUpperCase()} export`}</h1></div>
        <div className="job-state"><StateBadge state={job.state} /><span>Updated {date(job.updated_at)}</span></div>
      </section>
      {error && <div className="error banner" role="alert">{error}</div>}
      <section className="job-grid">
        <div className="panel progress-panel">
          <div className="panel-heading"><div><p className="eyebrow">Current fact</p><h2>Execution</h2></div>{!terminal && <button className="danger" onClick={async () => setJob(await cancelJob(jobID))}>Request cancellation</button>}</div>
          <div className="progress-stage"><span>{terminal ? job.state : job.progress?.stage ?? job.state}</span><strong>{terminal ? "Terminal state committed" : job.progress?.message ?? "Waiting for a worker"}</strong></div>
          {job.cancel_requested_at && <p className="notice">Cancellation requested {date(job.cancel_requested_at)}. The terminal state below remains authoritative.</p>}
          <div className="attempts">
            {job.attempts.map((attempt) => (
              <article key={attempt.attempt_number} className={`attempt ${attempt.attempt_number === job.current_attempt_number ? "current" : ""}`}>
                <span className="attempt-number">{String(attempt.attempt_number).padStart(2, "0")}</span><div><strong>Attempt {attempt.attempt_number}</strong><span>{date(attempt.started_at)} → {date(attempt.finished_at)}</span></div><StateBadge state={attempt.state} />
                {attempt.failure && <p>{attempt.failure.code}: {attempt.failure.message}</p>}
              </article>
            ))}
            {job.attempts.length === 0 && <div className="empty-state">No attempt has claimed this job yet.</div>}
          </div>
        </div>
        <div className="panel fact-panel"><p className="eyebrow">Normalized input</p><h2>Provenance</h2><dl><dt>Profile</dt><dd>{job.profile ?? "event-derived"}</dd><dt>Run A</dt><dd className="mono">{short(job.run_ids[0], 16)}</dd>{job.run_ids[1] && <><dt>Run B</dt><dd className="mono">{short(job.run_ids[1], 16)}</dd></>}<dt>Job</dt><dd className="mono">{short(job.job_id, 16)}</dd></dl>{provenance && <Provenance value={provenance} />}</div>
      </section>
      <section className="panel artifacts-panel">
        <div className="panel-heading"><div><p className="eyebrow">Exact outputs</p><h2>Artifacts</h2></div><span className="muted">Digest and size verified before commit</span></div>
        <div className="artifact-grid">
          {job.artifacts.map((artifact) => <ArtifactCard key={artifact.artifact_id} artifact={artifact} onReport={artifact.kind === "diff_html" ? () => setReport((value) => !value) : undefined} />)}
          {job.artifacts.length === 0 && <div className="empty-state">Artifacts appear only after an authoritative success.</div>}
        </div>
        {report && html && <div className="report-frame"><div><strong>Sandboxed HTML report</strong><button onClick={() => setReport(false)}>Close</button></div><iframe title="Tracewake diff report" sandbox="" src={artifactURL(html.artifact_id, true)} /></div>}
      </section>
    </>
  );
}

function Provenance({ value }: { value: Record<string, unknown> }) {
  return <div className="provenance-list">{Object.entries(value).slice(0, 8).map(([key, item]) => <div key={key}><span>{key.replaceAll("_", " ")}</span><strong className={typeof item === "string" && item.length > 24 ? "mono" : ""}>{typeof item === "object" ? JSON.stringify(item) : String(item)}</strong></div>)}</div>;
}

function ArtifactCard({ artifact, onReport }: { artifact: Artifact; onReport?: () => void }) {
  return (
    <article className="artifact-card"><span className="file-glyph">{artifact.kind.includes("html") ? "HTML" : artifact.kind.includes("json") ? "JSON" : "BIN"}</span><div><strong>{artifact.kind.replaceAll("_", " ")}</strong><span>{formatBytes(artifact.size)} · <span className="mono">{short(artifact.digest, 12)}</span></span></div>{onReport && <button onClick={onReport}>View safely</button>}<a href={artifactURL(artifact.artifact_id)} download>Download</a></article>
  );
}

function StateBadge({ state }: { state: string }) {
  const tone = state === "ready" || state === "succeeded" ? "ready" : state === "failed" || state === "invalid" || state === "fenced" ? "failed" : state === "cancelled" ? "cancelled" : "active";
  return <span className={`state-badge ${tone}`}><span className={`status-dot ${tone}`} />{state.replaceAll("_", " ")}</span>;
}
