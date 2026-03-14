import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { FormEvent, startTransition, useDeferredValue, useEffect, useState } from "react";
import { HashRouter, Link, Navigate, NavLink, Route, Routes, useParams, useSearchParams } from "react-router-dom";

import { ApiError, fetchRunDetail, fetchRuns } from "./api";
import { clearStoredSession, loadStoredSession, storeSession } from "./session";
import "./styles.css";
import { ORDERED_STAGES, type RegistrySession, type RunDetail, type RunLiveness, type RunProjection, type RunSummary, type SliceState, type StageSnapshot } from "./types";

const POLL_INTERVAL_MS = 15_000;
const DETAIL_TABS = ["overview", "timeline", "slices", "raw"] as const;

type DetailTab = (typeof DETAIL_TABS)[number];

function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        refetchOnWindowFocus: false,
      },
    },
  });
}

function getPollIntervalMs(): number {
  if (typeof window === "undefined") {
    return POLL_INTERVAL_MS;
  }

  return (window as Window & { __RUN_REGISTRY_POLL_INTERVAL_MS__?: number }).__RUN_REGISTRY_POLL_INTERVAL_MS__ ?? POLL_INTERVAL_MS;
}

function usePageVisibility(): boolean {
  const [isVisible, setIsVisible] = useState<boolean>(() => (typeof document === "undefined" ? true : !document.hidden));

  useEffect(() => {
    const handleVisibilityChange = () => {
      setIsVisible(!document.hidden);
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => {
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, []);

  return isVisible;
}

function useRunsQuery(session: RegistrySession, isVisible: boolean) {
  return useQuery({
    queryKey: ["runs", session.apiBaseUrl, session.token],
    queryFn: () => fetchRuns(session),
    refetchInterval: isVisible ? getPollIntervalMs() : false,
    refetchIntervalInBackground: false,
  });
}

function useRunDetailQuery(session: RegistrySession, runId: string, isVisible: boolean) {
  return useQuery({
    queryKey: ["runs", "detail", session.apiBaseUrl, session.token, runId],
    queryFn: () => fetchRunDetail(session, runId),
    refetchInterval: isVisible ? getPollIntervalMs() : false,
    refetchIntervalInBackground: false,
  });
}

function formatLabel(value: string | null | undefined): string {
  if (!value) {
    return "Unknown";
  }

  return value
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function formatTimestamp(timestamp: string | null): string {
  if (!timestamp) {
    return "—";
  }

  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(timestamp));
}

function formatRelativeTime(timestamp: string | null): string {
  if (!timestamp) {
    return "No signal";
  }

  const deltaMilliseconds = Date.now() - new Date(timestamp).getTime();
  const deltaSeconds = Math.round(deltaMilliseconds / 1000);

  if (Math.abs(deltaSeconds) < 60) {
    return `${Math.abs(deltaSeconds)}s ${deltaSeconds >= 0 ? "ago" : "ahead"}`;
  }

  const deltaMinutes = Math.round(deltaSeconds / 60);
  if (Math.abs(deltaMinutes) < 60) {
    return `${Math.abs(deltaMinutes)}m ${deltaMinutes >= 0 ? "ago" : "ahead"}`;
  }

  const deltaHours = Math.round(deltaMinutes / 60);
  if (Math.abs(deltaHours) < 24) {
    return `${Math.abs(deltaHours)}h ${deltaHours >= 0 ? "ago" : "ahead"}`;
  }

  const deltaDays = Math.round(deltaHours / 24);
  return `${Math.abs(deltaDays)}d ${deltaDays >= 0 ? "ago" : "ahead"}`;
}

function shortenPath(path: string | null): string {
  if (!path) {
    return "—";
  }

  const pieces = path.split("/").filter(Boolean);
  if (pieces.length <= 3) {
    return path;
  }

  return `.../${pieces.slice(-3).join("/")}`;
}

function normalizeDetailTab(value: string | null): DetailTab {
  if (value && DETAIL_TABS.includes(value as DetailTab)) {
    return value as DetailTab;
  }

  return "overview";
}

function matchesSearch(run: RunSummary, rawSearch: string): boolean {
  const search = rawSearch.trim().toLowerCase();
  if (!search) {
    return true;
  }

  const haystack = [
    run.run_id,
    run.feature_slug,
    run.workflow,
    run.status,
    run.liveness,
    run.current_stage,
    run.scheduler_status,
    run.active_slice,
    run.workspace_root,
    run.spec_path,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();

  return haystack.includes(search);
}

function statusTone(value: string): string {
  switch (value) {
    case "completed":
    case "verified":
    case "committed":
    case "pr_opened":
    case "active":
      return "good";
    case "running":
    case "in_progress":
    case "ready":
      return "warn";
    case "stale":
    case "blocked":
      return "stale";
    case "failed":
    case "stopped":
      return "bad";
    default:
      return "muted";
  }
}

function Badge({ value, label }: { value: string | null | undefined; label?: string }) {
  const safeValue = value ?? "unknown";
  return (
    <span className={`badge badge-${statusTone(safeValue)}`} aria-label={label ?? safeValue}>
      {formatLabel(safeValue)}
    </span>
  );
}

function EmptyState({ title, body }: { title: string; body: string }) {
  return (
    <section className="panel empty-state">
      <h2>{title}</h2>
      <p>{body}</p>
    </section>
  );
}

function ErrorState({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry: () => void;
}) {
  const message = error instanceof ApiError ? error.message : "Run registry request failed.";
  return (
    <section className="panel empty-state">
      <h2>Unable to load registry data</h2>
      <p>{message}</p>
      <button className="ghost-button" onClick={onRetry} type="button">
        Try again
      </button>
    </section>
  );
}

function LoadingState({ title, body }: { title: string; body: string }) {
  return (
    <section className="panel empty-state">
      <h2>{title}</h2>
      <p>{body}</p>
    </section>
  );
}

function ProjectionStrip({ title, values }: { title: string; values: string[] }) {
  return (
    <div className="projection-strip">
      <span className="projection-title">{title}</span>
      {values.length > 0 ? (
        <div className="chip-list">
          {values.map((value) => (
            <span className="chip" key={`${title}-${value}`}>
              {formatLabel(value)}
            </span>
          ))}
        </div>
      ) : (
        <span className="muted-copy">None</span>
      )}
    </div>
  );
}

function MetricCard({ label, value }: { label: string; value: string | number }) {
  return (
    <article className="metric-card">
      <span>{label}</span>
      <strong>{value}</strong>
    </article>
  );
}

function TokenGate({ onConnect }: { onConnect: (session: RegistrySession) => void }) {
  const [apiBaseUrl, setApiBaseUrl] = useState("http://localhost:8080");
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const nextSession = {
      apiBaseUrl: apiBaseUrl.trim().replace(/\/+$/, ""),
      token: token.trim(),
    };

    if (!nextSession.apiBaseUrl || !nextSession.token) {
      setError("Enter the run registry URL and bearer token to continue.");
      return;
    }

    setError(null);
    onConnect(nextSession);
  };

  return (
    <main className="gate-shell">
      <section className="gate-panel">
        <div className="eyebrow">Run Registry Console</div>
        <h1>Connect to the run registry</h1>
        <p>
          This console reads directly from the backend API. Your bearer token stays in
          <code>sessionStorage</code> for this browser tab only.
        </p>
        <form className="gate-form" onSubmit={handleSubmit}>
          <label>
            API base URL
            <input
              autoComplete="url"
              name="apiBaseUrl"
              onChange={(event) => setApiBaseUrl(event.target.value)}
              placeholder="http://localhost:8080"
              type="url"
              value={apiBaseUrl}
            />
          </label>
          <label>
            Bearer token
            <input
              autoComplete="off"
              name="token"
              onChange={(event) => setToken(event.target.value)}
              placeholder="Paste the registry API token"
              type="password"
              value={token}
            />
          </label>
          {error ? <p className="error-copy">{error}</p> : null}
          <button className="primary-button" type="submit">
            Open console
          </button>
        </form>
      </section>
    </main>
  );
}

function RunsDashboard({
  isVisible,
  session,
}: {
  isVisible: boolean;
  session: RegistrySession;
}) {
  const runsQuery = useRunsQuery(session, isVisible);
  const [search, setSearch] = useState("");
  const deferredSearch = useDeferredValue(search);
  const runs = runsQuery.data ?? [];
  const filteredRuns = runs.filter((run) => matchesSearch(run, deferredSearch));
  const activeCount = runs.filter((run) => run.liveness === "active").length;
  const staleCount = runs.filter((run) => run.liveness === "stale").length;
  const stoppedCount = runs.filter((run) => run.liveness === "stopped").length;

  return (
    <section className="page">
      <div className="page-head">
        <div>
          <div className="eyebrow">Operator Dashboard</div>
          <h1>AI-native runs</h1>
          <p>Track liveness, stage progression, and the latest slice state published by each run.</p>
        </div>
        <div className="toolbar">
          <label className="search-field">
            <span>Search</span>
            <input
              aria-label="Search runs"
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Run id, feature, stage, slice, path"
              value={search}
            />
          </label>
          <button className="ghost-button" onClick={() => runsQuery.refetch()} type="button">
            Refresh now
          </button>
        </div>
      </div>

      <div className="metrics-grid">
        <MetricCard label="Visible runs" value={filteredRuns.length} />
        <MetricCard label="Active" value={activeCount} />
        <MetricCard label="Stale" value={staleCount} />
        <MetricCard label="Stopped" value={stoppedCount} />
      </div>

      {runsQuery.isLoading ? (
        <LoadingState title="Loading runs" body="Fetching the latest run summaries from the registry." />
      ) : null}

      {runsQuery.isError ? <ErrorState error={runsQuery.error} onRetry={() => runsQuery.refetch()} /> : null}

      {!runsQuery.isLoading && !runsQuery.isError && filteredRuns.length === 0 ? (
        <EmptyState
          title={runs.length === 0 ? "No runs published yet" : "No runs matched this filter"}
          body={
            runs.length === 0
              ? "Start an ai-native workflow with registry publishing enabled to populate this view."
              : "Try a broader search to bring more runs back into view."
          }
        />
      ) : null}

      {!runsQuery.isLoading && !runsQuery.isError && filteredRuns.length > 0 ? (
        <section className="panel table-panel">
          <div className="panel-header">
            <div>
              <h2>Recent runs</h2>
              <p>{isVisible ? "Polling every 15 seconds while this tab is visible." : "Polling paused while the tab is hidden."}</p>
            </div>
          </div>
          <div className="table-wrap">
            <table className="runs-table">
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Stage</th>
                  <th>Scheduler</th>
                  <th>Slice</th>
                  <th>Status</th>
                  <th>Liveness</th>
                  <th>Updated</th>
                  <th>Workspace</th>
                </tr>
              </thead>
              <tbody>
                {filteredRuns.map((run) => (
                  <tr key={run.run_id}>
                    <td>
                      <Link className="run-link" to={`/runs/${run.run_id}`}>
                        <strong>{run.feature_slug ?? run.run_id}</strong>
                        <span>{run.run_id}</span>
                      </Link>
                    </td>
                    <td>{formatLabel(run.current_stage)}</td>
                    <td>{formatLabel(run.scheduler_status)}</td>
                    <td>{run.active_slice ?? "—"}</td>
                    <td>
                      <Badge value={run.status} />
                    </td>
                    <td>
                      <Badge value={run.liveness} />
                    </td>
                    <td>
                      <div className="timestamp-stack">
                        <time dateTime={run.updated_at}>{formatRelativeTime(run.updated_at)}</time>
                        <span>{formatTimestamp(run.updated_at)}</span>
                      </div>
                    </td>
                    <td title={run.workspace_root ?? undefined}>{shortenPath(run.workspace_root)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}
    </section>
  );
}

function OverviewTab({ detail }: { detail: RunDetail }) {
  const projection: RunProjection | null = detail.run_projection;
  const heartbeat = detail.metadata.heartbeat;

  return (
    <div className="detail-grid">
      <section className="panel detail-card">
        <h2>Lifecycle</h2>
        <dl className="detail-list">
          <div>
            <dt>Workflow</dt>
            <dd>{detail.workflow}</dd>
          </div>
          <div>
            <dt>Status</dt>
            <dd>
              <Badge value={detail.status} />
            </dd>
          </div>
          <div>
            <dt>Liveness</dt>
            <dd>
              <Badge value={detail.liveness} />
            </dd>
          </div>
          <div>
            <dt>Current stage</dt>
            <dd>{formatLabel(detail.current_stage)}</dd>
          </div>
          <div>
            <dt>Scheduler</dt>
            <dd>{formatLabel(detail.scheduler_status)}</dd>
          </div>
          <div>
            <dt>Active slice</dt>
            <dd>{detail.active_slice ?? "—"}</dd>
          </div>
        </dl>
      </section>

      <section className="panel detail-card">
        <h2>Paths</h2>
        <dl className="detail-list">
          <div>
            <dt>Spec</dt>
            <dd title={detail.spec_path ?? undefined}>{detail.spec_path ?? "—"}</dd>
          </div>
          <div>
            <dt>Workspace</dt>
            <dd title={detail.workspace_root ?? undefined}>{detail.workspace_root ?? "—"}</dd>
          </div>
          <div>
            <dt>Run directory</dt>
            <dd title={detail.run_dir ?? undefined}>{detail.run_dir ?? "—"}</dd>
          </div>
        </dl>
      </section>

      <section className="panel detail-card">
        <h2>Timestamps</h2>
        <dl className="detail-list">
          <div>
            <dt>Created</dt>
            <dd>{formatTimestamp(detail.created_at)}</dd>
          </div>
          <div>
            <dt>Updated</dt>
            <dd>{formatTimestamp(detail.updated_at)}</dd>
          </div>
          <div>
            <dt>Heartbeat</dt>
            <dd>{formatTimestamp(detail.last_heartbeat_at)}</dd>
          </div>
          <div>
            <dt>Expires</dt>
            <dd>{formatTimestamp(detail.expires_at)}</dd>
          </div>
        </dl>
      </section>

      <section className="panel detail-card">
        <h2>Run projection</h2>
        {projection ? (
          <div className="projection-grid">
            <ProjectionStrip title="Completed" values={projection.completed_steps} />
            <ProjectionStrip title="In progress" values={projection.in_progress_steps} />
            <ProjectionStrip title="Next" values={projection.next_executable_steps} />
            <div className="projection-strip">
              <span className="projection-title">Blocked</span>
              {projection.blocked_steps.length > 0 ? (
                <div className="blocked-list">
                  {projection.blocked_steps.map((step) => (
                    <article className="blocked-item" key={`${step.step}-${step.reason}`}>
                      <strong>{formatLabel(step.step)}</strong>
                      <span>{step.reason}</span>
                    </article>
                  ))}
                </div>
              ) : (
                <span className="muted-copy">None</span>
              )}
            </div>
          </div>
        ) : (
          <p className="muted-copy">No run projection snapshot has been published for this run yet.</p>
        )}
      </section>

      <section className="panel detail-card">
        <h2>Metadata</h2>
        <dl className="detail-list">
          <div>
            <dt>Metadata keys</dt>
            <dd>{Object.keys(detail.metadata).length > 0 ? Object.keys(detail.metadata).join(", ") : "—"}</dd>
          </div>
          <div>
            <dt>Heartbeat payload</dt>
            <dd>{heartbeat ? JSON.stringify(heartbeat) : "—"}</dd>
          </div>
        </dl>
      </section>
    </div>
  );
}

function TimelineTab({ detail }: { detail: RunDetail }) {
  const stages = ORDERED_STAGES.map((stage) => {
    const snapshot: StageSnapshot | undefined = detail.stage_status[stage];
    return {
      stage,
      status: snapshot?.status ?? "pending",
      artifacts: snapshot?.artifacts ?? [],
      notes: snapshot?.notes ?? [],
      isCurrent: detail.current_stage === stage,
    };
  });

  return (
    <div className="timeline-grid">
      {stages.map((stage) => (
        <article className={`panel stage-card${stage.isCurrent ? " stage-card-current" : ""}`} key={stage.stage}>
          <div className="stage-card-head">
            <div>
              <span className="eyebrow">Stage</span>
              <h2>{formatLabel(stage.stage)}</h2>
            </div>
            <Badge label={`Stage status ${stage.status}`} value={stage.status} />
          </div>
          <p className="muted-copy">{stage.isCurrent ? "Current stage in the registry snapshot." : "Latest known stage snapshot."}</p>
          <div className="stage-section">
            <strong>Artifacts</strong>
            {stage.artifacts.length > 0 ? (
              <ul className="simple-list">
                {stage.artifacts.map((artifact) => (
                  <li key={artifact}>{artifact}</li>
                ))}
              </ul>
            ) : (
              <p className="muted-copy">No artifacts published.</p>
            )}
          </div>
          <div className="stage-section">
            <strong>Notes</strong>
            {stage.notes.length > 0 ? (
              <ul className="simple-list">
                {stage.notes.map((note) => (
                  <li key={note}>{note}</li>
                ))}
              </ul>
            ) : (
              <p className="muted-copy">No notes published.</p>
            )}
          </div>
        </article>
      ))}
    </div>
  );
}

function SliceCard({ slice }: { slice: SliceState }) {
  return (
    <article className="panel slice-card">
      <div className="slice-card-head">
        <div>
          <span className="eyebrow">Slice</span>
          <h2>{slice.slice_id}</h2>
        </div>
        <Badge value={slice.status} />
      </div>
      <dl className="detail-list">
        <div>
          <dt>Stage</dt>
          <dd>{formatLabel(slice.current_stage)}</dd>
        </div>
        <div>
          <dt>Branch</dt>
          <dd>{slice.branch_name ?? "—"}</dd>
        </div>
        <div>
          <dt>Worktree</dt>
          <dd title={slice.worktree_path ?? undefined}>{slice.worktree_path ?? "—"}</dd>
        </div>
        <div>
          <dt>Commit</dt>
          <dd>{slice.commit_sha ?? "—"}</dd>
        </div>
        <div>
          <dt>PR</dt>
          <dd>
            {slice.pr_url ? (
              <a href={slice.pr_url} rel="noreferrer" target="_blank">
                {slice.pr_url}
              </a>
            ) : (
              "—"
            )}
          </dd>
        </div>
        <div>
          <dt>Updated</dt>
          <dd>{formatTimestamp(slice.updated_at)}</dd>
        </div>
      </dl>
      <div className="slice-footer">
        <strong>Block reason</strong>
        <p>{slice.block_reason ?? "Not blocked."}</p>
      </div>
    </article>
  );
}

function SlicesTab({ detail }: { detail: RunDetail }) {
  const slices = Object.values(detail.slice_states).sort((left, right) => left.slice_id.localeCompare(right.slice_id));

  if (slices.length === 0) {
    return <EmptyState title="No slice snapshots yet" body="This run has not published any slice execution state." />;
  }

  return (
    <div className="slice-grid">
      {slices.map((slice) => (
        <SliceCard key={slice.slice_id} slice={slice} />
      ))}
    </div>
  );
}

function RawTab({ detail }: { detail: RunDetail }) {
  return (
    <section className="panel raw-panel">
      <h2>Raw registry payload</h2>
      <pre>{JSON.stringify(detail, null, 2)}</pre>
    </section>
  );
}

function RunDetailPage({
  isVisible,
  session,
}: {
  isVisible: boolean;
  session: RegistrySession;
}) {
  const { runId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();

  if (!runId) {
    return <Navigate replace to="/" />;
  }

  const detailQuery = useRunDetailQuery(session, runId, isVisible);
  const activeTab = normalizeDetailTab(searchParams.get("tab"));

  return (
    <section className="page">
      <div className="page-head">
        <div>
          <Link className="back-link" to="/">
            Back to runs
          </Link>
          <div className="eyebrow">Run detail</div>
          <h1>{runId}</h1>
        </div>
        <div className="toolbar">
          <button className="ghost-button" onClick={() => detailQuery.refetch()} type="button">
            Refresh now
          </button>
        </div>
      </div>

      {detailQuery.isLoading ? (
        <LoadingState title="Loading run detail" body="Fetching the latest rich registry snapshot for this run." />
      ) : null}

      {detailQuery.isError ? <ErrorState error={detailQuery.error} onRetry={() => detailQuery.refetch()} /> : null}

      {detailQuery.data ? (
        <>
          <section className="panel detail-hero">
            <div className="hero-copy">
              <div className="eyebrow">Feature</div>
              <h2>{detailQuery.data.feature_slug ?? detailQuery.data.run_id}</h2>
              <p>{detailQuery.data.workspace_root ?? "No workspace path published."}</p>
            </div>
            <div className="hero-badges">
              <Badge value={detailQuery.data.status} />
              <Badge value={detailQuery.data.liveness} />
              <Badge value={detailQuery.data.scheduler_status} />
            </div>
          </section>

          <nav aria-label="Run detail tabs" className="tab-bar">
            {DETAIL_TABS.map((tab) => (
              <button
                className={`tab-button${activeTab === tab ? " tab-button-active" : ""}`}
                key={tab}
                onClick={() => setSearchParams({ tab })}
                type="button"
              >
                {formatLabel(tab)}
              </button>
            ))}
          </nav>

          {activeTab === "overview" ? <OverviewTab detail={detailQuery.data} /> : null}
          {activeTab === "timeline" ? <TimelineTab detail={detailQuery.data} /> : null}
          {activeTab === "slices" ? <SlicesTab detail={detailQuery.data} /> : null}
          {activeTab === "raw" ? <RawTab detail={detailQuery.data} /> : null}
        </>
      ) : null}
    </section>
  );
}

function AppShell({
  isVisible,
  onDisconnect,
  session,
}: {
  isVisible: boolean;
  onDisconnect: () => void;
  session: RegistrySession;
}) {
  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-lockup">
          <div className="brand-badge">AR</div>
          <div>
            <div className="eyebrow">Run Registry</div>
            <strong>Operations console</strong>
          </div>
        </div>
        <nav className="topnav">
          <NavLink end to="/">
            Runs
          </NavLink>
        </nav>
        <div className="session-strip">
          <span className={`live-indicator ${isVisible ? "live-indicator-on" : ""}`}>
            {isVisible ? "Polling live" : "Paused while hidden"}
          </span>
          <span className="session-endpoint" title={session.apiBaseUrl}>
            {session.apiBaseUrl}
          </span>
          <button className="ghost-button" onClick={onDisconnect} type="button">
            Disconnect
          </button>
        </div>
      </header>

      <main className="main-shell">
        <Routes>
          <Route element={<RunsDashboard isVisible={isVisible} session={session} />} path="/" />
          <Route element={<RunDetailPage isVisible={isVisible} session={session} />} path="/runs/:runId" />
          <Route element={<Navigate replace to="/" />} path="*" />
        </Routes>
      </main>
    </div>
  );
}

export default function App() {
  const [queryClient] = useState(createQueryClient);
  const [session, setSession] = useState<RegistrySession | null>(() => loadStoredSession());
  const isVisible = usePageVisibility();

  const handleConnect = (nextSession: RegistrySession) => {
    storeSession(nextSession);
    startTransition(() => {
      setSession(nextSession);
    });
  };

  const handleDisconnect = () => {
    clearStoredSession();
    queryClient.clear();
    startTransition(() => {
      setSession(null);
    });
  };

  return (
    <QueryClientProvider client={queryClient}>
      <HashRouter future={{ v7_relativeSplatPath: true, v7_startTransition: true }}>
        {session ? <AppShell isVisible={isVisible} onDisconnect={handleDisconnect} session={session} /> : <TokenGate onConnect={handleConnect} />}
      </HashRouter>
    </QueryClientProvider>
  );
}
