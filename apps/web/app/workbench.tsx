"use client";

import { useCallback, useEffect, useState } from "react";

type Decision = {
  run_id?: string;
  model_id?: string;
  model_version?: string;
  rules_version?: string;
  code_version?: string;
  snapshot_id?: string;
  data_as_of?: string;
  action: "NO_TRADE" | "WAIT" | "ENTER";
  reasons: string[];
  output?: Record<string, string | number>;
  detail?: string;
  run_mode?: "LIVE" | "SCENARIO";
};

type DecisionHistoryItem = Decision & { recorded_at: string; scenario: string };

type AttributionTask = {
  task_id: string;
  created_at: string;
  reaction_lag_seconds: number;
  reaction_timing_interpretation: string;
  cross_asset_coherence: string;
  confidence: number;
  review_status: string;
  counterfactual_replay_link: string;
  retained_as_reusable_sample: boolean;
  action: string;
  execution_enabled: boolean;
  signal: {
    signal_id: string;
    kind: string;
    severity: string;
    observed_as_of: string;
    snapshot_id: string;
    replay_manifest_hash: string;
    candidates: Array<{ cause_id: string; summary: string; confidence: number }>;
  };
};

type Delivery = {
  delivery_id: string;
  connection_id: string;
  stream_event_id: string;
  attempted_at: string;
  outcome: string;
};

type ModelVersion = {
  version: string;
  artifact_hash: string;
  data_manifest_hash: string;
  trained_at: string;
  validation_report_hash: string | null;
  parent_version: string | null;
  is_local_champion: boolean;
  calibration_status: string;
  live_eligible: boolean;
};

type ControlPlaneData = {
  run_mode: "SCENARIO";
  scope: "LOCAL";
  verification: "UNVERIFIED";
  execution_enabled: false;
  checked_at: string;
  attribution: { tasks: AttributionTask[] } | null;
  deliveries: { deliveries: Delivery[] } | null;
  governance: {
    versions: { model_id: string; versions: ModelVersion[]; live_enabled: boolean } | null;
    champion: { champion: ModelVersion; frozen_for_session: boolean; live_enabled: boolean } | null;
    validation: { calibration_status: string; report_hash: string | null; slices: unknown[]; conclusion: string; live_eligible: boolean } | null;
  };
  audit: {
    decisions: { decisions: Decision[] } | null;
    replays: { manifests: Array<{ manifest_hash: string; as_of: string; entries: Array<{ logical_key: string; record_id: string; content_hash: string }> }> } | null;
    integrity: { backend: "sqlite" | "postgresql"; schema_version: string; quick_check: string[]; foreign_key_violations: number; status: string } | null;
  };
  errors: Array<{ area: string; error: string }>;
};

export type PlatformStatus = {
  connected: boolean;
  model: { model_id: string; name: string; version: string } | null;
  market: {
    quality: string;
    reason: string;
    execution_enabled: boolean;
    data_asof?: string;
    stale_fields?: string[];
  } | null;
  events: { status: string; events: unknown[]; event_cleared: boolean; message?: string } | null;
  modelHealth: { status: string; message?: string; models?: string[] } | null;
  capabilities: { provider: string; status?: string; quality: string; results: unknown[] } | null;
  checkedAt: string;
};

type OperationsData = {
  overview: {
    verification: string;
    action: string;
    execution_enabled: boolean;
    replay: { status: string; detail: string; item_count?: number };
    risk_lock: { status: string; detail: string; item_count?: number };
    alerts: { status: string; detail: string; item_count?: number };
  };
  scenarios: Array<{
    scenario_id: string;
    title: string;
    summary: string;
    verification: string;
    action: string;
    assessment: { state: string; reasons: string[]; next_checkpoint?: string; rerun_at?: string };
    event: {
      event_id: string;
      kind: string;
      severity: string;
      first_seen_at: string;
      corroborating_sources: number;
      cross_asset_confirmed: boolean;
    };
    replay_manifest: {
      as_of: string;
      manifest_hash: string;
      entries: Array<{ logical_key: string; record_id: string; content_hash: string }>;
    };
  }>;
  alerts: {
    verification: string;
    execution_enabled: boolean;
    alerts: Array<{
      alert_id: string;
      status: string;
      deduplicated_count: number;
      escalation_level: number;
      created_at: string;
      candidate: {
        priority: string;
        direction: string;
        evidence: string[];
        action: string;
        invalidation_conditions: string[];
        rerun_trigger: string;
        snapshot_id: string;
      };
    }>;
  };
  assurance: {
    equitySession: {
      verification: string;
      action: string;
      status: string;
      is_early_close: boolean;
      closes_at: string;
      anchor_at: string;
    };
    quoteQuality: {
      verification: string;
      action: string;
      quality: string;
      freeze: boolean;
      reasons: string[];
    };
    economics: {
      verification: string;
      action: string;
      manual_execution_only: boolean;
      opening_value: { net_cashflow: number | null; is_executable: boolean };
      risk: { max_loss: number | null; tail_loss_cvar: number; scenario_expected_pnl: number; risk_gate_cleared: boolean };
    };
  };
};

const scenarios = [
  {
    id: "live",
    kicker: "Fail-closed baseline",
    title: "Live / unverified",
    description: "No verified feed or option chain. Demonstrates the production-safe default.",
    badge: "NO_TRADE",
  },
  {
    id: "cpi",
    kicker: "Scheduled event",
    title: "CPI Risk Lock",
    description: "Data is present, but CPI timing and expanding tails keep the strategy locked.",
    badge: "RISK LOCK",
  },
  {
    id: "cleared",
    kicker: "Post-event demo",
    title: "Event-cleared map",
    description: "All demo gates clear. Produces a non-executable strike map for review.",
    badge: "WAIT",
  },
] as const;

const seededDecision: Decision = {
  action: "NO_TRADE",
  reasons: ["DATA_CAPABILITY_NOT_VERIFIED", "EVENT_PENDING", "OPTION_CHAIN_UNUSABLE"],
};

const reasonCopy: Record<string, string> = {
  API_UNREACHABLE: "Decision service cannot be reached",
  DATA_CAPABILITY_NOT_VERIFIED: "Provider capabilities have not been verified",
  STALE_MARKET_DATA: "Market data is stale or unavailable",
  EVENT_PENDING: "Event stability window has not cleared",
  OPTION_CHAIN_UNUSABLE: "Option chain is not executable-quality",
  TAIL_EXPANDING: "Implied tail range is still expanding",
  NEXT_EVENT_TOO_CLOSE: "A major event falls inside the holding window",
  NEGATIVE_EDGE: "Estimated edge does not clear the threshold",
  MODEL_VERSION_NOT_LOADED: "Governed champion artifact is not loaded by the decision runner",
};

function StatusDot({ tone }: { tone: "good" | "warn" | "bad" }) {
  return <span className={`status-dot ${tone}`} aria-hidden="true" />;
}

function Icon({ name }: { name: "grid" | "pulse" | "event" | "history" | "model" | "settings" }) {
  const paths = {
    grid: <><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></>,
    pulse: <path d="M3 12h4l2-6 4 12 2-6h6" />,
    event: <><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18"/></>,
    history: <><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/></>,
    model: <><path d="M12 3 4 7v10l8 4 8-4V7l-8-4Z"/><path d="m4 7 8 4 8-4M12 11v10"/></>,
    settings: <><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9A1.7 1.7 0 0 0 21 10h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/></>,
  };
  return <svg className="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7">{paths[name]}</svg>;
}

export default function Workbench({ initialStatus }: { initialStatus: PlatformStatus }) {
  const [activeScenario, setActiveScenario] = useState("live");
  const [decision, setDecision] = useState<Decision>(seededDecision);
  const [loading, setLoading] = useState(false);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [operations, setOperations] = useState<OperationsData | null>(null);
  const [operationsError, setOperationsError] = useState<string | null>(null);
  const [feedbackBusy, setFeedbackBusy] = useState<string | null>(null);
  const [decisionHistory, setDecisionHistory] = useState<DecisionHistoryItem[]>([]);
  const [controlPlane, setControlPlane] = useState<ControlPlaneData | null>(null);
  const [controlPlaneError, setControlPlaneError] = useState<string | null>(null);
  const [reviewBusy, setReviewBusy] = useState<string | null>(null);
  const marketGreen = initialStatus.market?.quality === "GREEN";
  const eventCleared =
    !loading &&
    !requestError &&
    decision.run_mode === "SCENARIO" &&
    decision.action === "WAIT" &&
    decision.output !== undefined;
  const riskLocked = !eventCleared;

  const loadOperations = useCallback(async () => {
    try {
      const response = await fetch("/api/operations", { cache: "no-store" });
      const result = (await response.json()) as OperationsData & { detail?: string };
      if (!response.ok) throw new Error(result.detail ?? "Operations request failed");
      setOperations(result);
      setOperationsError(null);
    } catch (error) {
      setOperationsError(error instanceof Error ? error.message : "Operations API unreachable");
    }
  }, []);

  useEffect(() => {
    void loadOperations();
  }, [loadOperations]);

  const loadControlPlane = useCallback(async () => {
    try {
      const response = await fetch("/api/control-plane", { cache: "no-store" });
      const result = (await response.json()) as ControlPlaneData & { detail?: string };
      if (!response.ok) throw new Error(result.detail ?? "Control-plane request failed");
      setControlPlane(result);
      setControlPlaneError(null);
    } catch (error) {
      setControlPlaneError(error instanceof Error ? error.message : "Control-plane API unreachable");
    }
  }, []);

  useEffect(() => {
    void loadControlPlane();
    const timer = window.setInterval(() => void loadControlPlane(), 15_000);
    return () => window.clearInterval(timer);
  }, [loadControlPlane]);

  useEffect(() => {
    try {
      const saved = window.localStorage.getItem("marketpilot-local-decision-history-v1");
      if (saved) setDecisionHistory((JSON.parse(saved) as DecisionHistoryItem[]).slice(0, 12));
    } catch {
      window.localStorage.removeItem("marketpilot-local-decision-history-v1");
    }
  }, []);

  async function submitFeedback(alertId: string, kind: string) {
    setFeedbackBusy(kind);
    setOperationsError(null);
    try {
      const response = await fetch("/api/operations/feedback", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ alertId, kind }),
      });
      const result = (await response.json()) as { detail?: string };
      if (!response.ok) throw new Error(result.detail ?? "Feedback request failed");
      await loadOperations();
    } catch (error) {
      setOperationsError(error instanceof Error ? error.message : "Local feedback failed");
    } finally {
      setFeedbackBusy(null);
    }
  }

  async function runScenario(id: string) {
    setActiveScenario(id);
    setLoading(true);
    setRequestError(null);
    try {
      const response = await fetch("/api/decision", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ scenario: id }),
      });
      const result = (await response.json()) as Decision;
      setDecision(result);
      if (!response.ok) {
        setRequestError(result.detail ?? "Decision request failed");
      } else {
        const item = { ...result, recorded_at: new Date().toISOString(), scenario: id };
        setDecisionHistory((current) => {
          const updated = [item, ...current.filter((entry) => entry.run_id !== item.run_id)].slice(0, 12);
          window.localStorage.setItem("marketpilot-local-decision-history-v1", JSON.stringify(updated));
          return updated;
        });
      }
    } catch {
      setDecision({ action: "NO_TRADE", reasons: ["API_UNREACHABLE"] });
      setRequestError("Decision service unreachable. System remained fail-closed.");
    } finally {
      setLoading(false);
    }
  }

  async function reviewAttribution(taskId: string, status: string) {
    setReviewBusy(taskId);
    setControlPlaneError(null);
    try {
      const response = await fetch(`/api/control-plane/attribution/${encodeURIComponent(taskId)}/review`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ status }),
      });
      const result = (await response.json()) as { detail?: string };
      if (!response.ok) throw new Error(result.detail ?? "Attribution review failed");
      await loadControlPlane();
    } catch (error) {
      setControlPlaneError(error instanceof Error ? error.message : "Attribution review API unreachable");
    } finally {
      setReviewBusy(null);
    }
  }

  const legs = [
    ["Long put", decision.output?.long_put],
    ["Short put", decision.output?.short_put],
    ["Short call", decision.output?.short_call],
    ["Long call", decision.output?.long_call],
  ];
  const serverDecisionHistory = controlPlane?.audit.decisions?.decisions ?? [];
  const visibleDecisionHistory: DecisionHistoryItem[] = serverDecisionHistory.length
    ? serverDecisionHistory.map((item) => ({
        ...item,
        recorded_at: item.data_as_of ?? controlPlane?.checked_at ?? new Date(0).toISOString(),
        scenario: item.run_mode ?? "SCENARIO",
      }))
    : decisionHistory;
  const auditBackend = controlPlane?.audit.integrity?.backend?.toUpperCase() ?? "LOCAL";

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark">M</span><span>MarketPilot<small>Decision intelligence</small></span></div>
        <nav aria-label="Primary navigation">
          <a className="active" href="#overview"><Icon name="grid" />Overview</a>
          <a href="#decision"><Icon name="pulse" />Decision desk</a>
          <a href="#events"><Icon name="event" />Events</a>
          <a href="#operations"><Icon name="pulse" />Operations</a>
          <a href="#decision-history"><Icon name="history" />Decision log</a>
          <a href="#attribution"><Icon name="event" />Attribution</a>
          <a href="#governance"><Icon name="model" />Models</a>
        </nav>
        <div className="sidebar-bottom">
          <a href="#system"><Icon name="settings" />System status</a>
          <div className="operator"><span>OP</span><p>Read-only operator<small>Manual execution</small></p></div>
        </div>
      </aside>

      <div className="workspace">
        <div className="warning-bar"><strong>DEMO / UNVERIFIED</strong><span>Not investment advice. Market data and model calibration are not production-verified.</span><b>MANUAL ONLY</b></div>
        <header className="topbar">
          <div><p className="breadcrumb">WORKSPACE / <span>OVERVIEW</span></p><h1>Decision workbench</h1></div>
          <div className="market-clock"><span>MARKET STATUS</span><strong><StatusDot tone="warn" /> DATA UNVERIFIED</strong><small>Checked {new Intl.DateTimeFormat("en-US", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "UTC" }).format(new Date(initialStatus.checkedAt))} UTC</small></div>
        </header>

        <main id="overview">
          <section className="status-grid" id="system" aria-label="Platform status">
            <article className="status-card"><div><StatusDot tone={initialStatus.connected ? "good" : "bad"} /><span>API connection</span></div><strong>{initialStatus.connected ? "Connected" : "Unreachable"}</strong><small>{initialStatus.connected ? "FastAPI / health OK" : "Fail-closed mode active"}</small></article>
            <article className="status-card"><div><StatusDot tone={marketGreen ? "good" : "bad"} /><span>Data quality</span></div><strong>{initialStatus.market?.quality ?? "Not connected"}</strong><small>{initialStatus.market?.reason ?? "No market-state response"}</small></article>
            <article className="status-card" id="model"><div><StatusDot tone="warn" /><span>Model state</span></div><strong>{initialStatus.modelHealth?.status ?? "Unavailable"}</strong><small>{initialStatus.model ? `${initialStatus.model.name} ${initialStatus.model.version}` : "Registry unavailable"}</small></article>
            <article className="status-card"><div><StatusDot tone={riskLocked ? "bad" : "good"} /><span>Risk Lock</span></div><strong>{riskLocked ? "Engaged" : "Event cleared"}</strong><small>{riskLocked ? "New entries suppressed" : "Review-only strike map"}</small></article>
          </section>

          <section className="scenario-section" aria-labelledby="scenario-heading">
            <div className="section-heading"><div><p className="eyebrow">CONTROLLED DEMONSTRATIONS</p><h2 id="scenario-heading">Choose a decision scenario</h2></div><p>Each scenario calls the live decision API through a server-side proxy. Outputs remain non-executable.</p></div>
            <div className="scenario-grid">
              {scenarios.map((scenario) => (
                <button key={scenario.id} className={`scenario-card ${activeScenario === scenario.id ? "selected" : ""}`} onClick={() => runScenario(scenario.id)} disabled={loading}>
                  <span className="scenario-radio" aria-hidden="true" /><div><small>{scenario.kicker}</small><strong>{scenario.title}</strong><p>{scenario.description}</p></div><b>{scenario.badge}</b>
                </button>
              ))}
            </div>
          </section>

          <section className="decision-grid" id="decision">
            <article className={`decision-brief action-${decision.action.toLowerCase().replace("_", "-")}`}>
              <div className="panel-title"><div><p className="eyebrow">CURRENT DECISION</p><h2>Safety-gated brief</h2></div><span className="read-only">READ ONLY</span></div>
              <div className="action-block"><span>{loading ? "RUNNING GATES…" : decision.action.replace("_", " ")}</span><p>{decision.action === "WAIT" ? "A strike map is available for human review. No order is created." : "No new position. Re-evaluate only after every blocking gate clears."}</p></div>
              {requestError && <div className="inline-alert">{requestError}</div>}
              <div className="reason-list">
                <h3>Gate rationale</h3>
                {decision.reasons.length ? decision.reasons.map((reason) => <div key={reason}><span>×</span><p><strong>{reasonCopy[reason] ?? reason.replaceAll("_", " ")}</strong><small>{reason}</small></p></div>) : <div className="clear-reason"><span>✓</span><p><strong>All configured demo gates cleared</strong><small>Decision remains WAIT because baseline is not calibrated</small></p></div>}
              </div>
            </article>

            <article className="legs-panel">
              <div className="panel-title"><div><p className="eyebrow">SPXW 0DTE / IRON CONDOR</p><h2>Review-only strike map</h2></div><span className="demo-chip">DEMO</span></div>
              <div className="underlying"><span>REFERENCE CENTER</span><strong>{typeof decision.output?.reference_center === "number" ? decision.output.reference_center.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "—"}</strong><small>Illustrative input, not a live quote</small></div>
              <div className="legs-table">
                <div className="table-head"><span>LEG</span><span>STRIKE</span><span>STATE</span></div>
                {legs.map(([label, strike], index) => <div className="table-row" key={label}><span><i className={index === 0 || index === 3 ? "long" : "short"} />{label}</span><strong>{strike ?? "—"}</strong><small>{strike ? "REVIEW" : "LOCKED"}</small></div>)}
              </div>
              <p className="manual-note"><strong>Manual execution boundary</strong>This workspace never submits, stages, or routes an order. Validate every quote in Webull before any separate manual action.</p>
            </article>
          </section>

          <section className="lower-grid">
            <article className="timeline-panel" id="events">
              <div className="panel-title"><div><p className="eyebrow">EVENT INTELLIGENCE</p><h2>Stability timeline</h2></div><span className={eventCleared ? "state-clear" : "state-lock"}>{eventCleared ? "CLEARED" : "RISK LOCK"}</span></div>
              <div className="timeline">
                <div className="timeline-item done"><span /><time>08:15 ET</time><p><strong>Pre-event snapshot</strong><small>Inputs frozen with first-seen timestamps</small></p></div>
                <div className="timeline-item alert"><span /><time>08:30 ET</time><p><strong>CPI release window</strong><small>Directional output suppressed</small></p></div>
                <div className={`timeline-item ${eventCleared ? "done" : "pending"}`}><span /><time>08:32 ET</time><p><strong>Cross-asset confirmation</strong><small>{eventCleared ? "Demo reaction checks coherent" : "Awaiting stable ES / VIX response"}</small></p></div>
                <div className={`timeline-item ${eventCleared ? "done" : "pending"}`}><span /><time>08:45 ET</time><p><strong>Event-cleared gate</strong><small>{eventCleared ? "Stability window satisfied" : "Not yet eligible for review"}</small></p></div>
              </div>
            </article>

            <article className="provenance-panel" id="provenance">
              <div className="panel-title"><div><p className="eyebrow">REPRODUCIBILITY</p><h2>Decision provenance</h2></div><span className="hash-icon">#</span></div>
              <dl>
                <div><dt>Run ID</dt><dd title={decision.run_id}>{decision.run_id ? decision.run_id.slice(0, 18) + "…" : "Pending API run"}</dd></div>
                <div><dt>Snapshot</dt><dd title={decision.snapshot_id}>{decision.snapshot_id ? decision.snapshot_id.slice(0, 18) + "…" : "Not available"}</dd></div>
                <div><dt>Model</dt><dd>{decision.model_version ?? initialStatus.model?.version ?? "Unavailable"}</dd></div>
                <div><dt>Rules</dt><dd>{decision.rules_version ?? "rules-v1"}</dd></div>
                <div><dt>Code</dt><dd>{decision.code_version ?? "development-unpinned"}</dd></div>
                <div><dt>Data as of</dt><dd>{decision.data_as_of ? new Date(decision.data_as_of).toLocaleString() : "No verified timestamp"}</dd></div>
                <div><dt>Provider probe</dt><dd>{initialStatus.capabilities?.status ?? initialStatus.capabilities?.quality ?? "NOT RUN"}</dd></div>
              </dl>
              <p>Snapshot, model, rules, code, and source time travel together. Missing evidence keeps the result fail-closed.</p>
            </article>
          </section>

          <section className="operations-section" id="operations" aria-labelledby="operations-heading">
            <div className="section-heading operations-heading">
              <div><p className="eyebrow">LOCAL OPERATIONS LOOP</p><h2 id="operations-heading">Replay, Risk Lock &amp; alert feedback</h2></div>
              <div className="ops-boundaries"><span>UNVERIFIED</span><span>LOCAL AUDIT</span><span>NO EXTERNAL SIDE EFFECTS</span></div>
            </div>
            {operationsError && <div className="inline-alert ops-error">{operationsError}</div>}
            {!operations ? (
              <div className="ops-loading">Loading server-derived operations state…</div>
            ) : (
              <>
                <div className="ops-summary">
                  <article><span>Overview action</span><strong>{operations.overview.action}</strong><small>{operations.overview.verification} · execution disabled</small></article>
                  <article><span>Replay fixtures</span><strong>{operations.overview.replay.item_count ?? 0}</strong><small>{operations.overview.replay.status} · deterministic local manifests</small></article>
                  <article><span>Risk Locks</span><strong>{operations.overview.risk_lock.item_count ?? 0}</strong><small>{operations.overview.risk_lock.status} · evidence-gated</small></article>
                  <article><span>Demo alerts</span><strong>{operations.overview.alerts.item_count ?? 0}</strong><small>{operations.overview.alerts.status} · append-only local audit</small></article>
                </div>

                <div className="assurance-grid" aria-label="Scenario assurance checks">
                  <article>
                    <div><span>Calendar contract</span><b>{operations.assurance.equitySession.action}</b></div>
                    <strong>{operations.assurance.equitySession.is_early_close ? "EARLY CLOSE" : operations.assurance.equitySession.status}</strong>
                    <p>Anchor {new Date(operations.assurance.equitySession.anchor_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", timeZone: "America/New_York" })} ET · actual session close</p>
                  </article>
                  <article>
                    <div><span>Dual-source quality</span><b>{operations.assurance.quoteQuality.action}</b></div>
                    <strong className="assurance-red">{operations.assurance.quoteQuality.quality} / {operations.assurance.quoteQuality.freeze ? "FROZEN" : "OPEN"}</strong>
                    <p>{operations.assurance.quoteQuality.reasons.join(" · ")}</p>
                  </article>
                  <article>
                    <div><span>Conservative economics</span><b>{operations.assurance.economics.action}</b></div>
                    <strong>${operations.assurance.economics.opening_value.net_cashflow?.toFixed(2) ?? "—"} demo net credit</strong>
                    <p>Max loss ${operations.assurance.economics.risk.max_loss ?? "UNKNOWN"} · CVaR ${operations.assurance.economics.risk.tail_loss_cvar.toFixed(2)}</p>
                  </article>
                </div>

                <div className="ops-body">
                  <article className="replay-panel">
                    <div className="panel-title"><div><p className="eyebrow">POINT-IN-TIME FIXTURES</p><h2>Replay manifests &amp; evidence</h2></div><span className="demo-chip">LOCAL</span></div>
                    <div className="replay-list">
                      {operations.scenarios.map((scenario) => (
                        <details key={scenario.scenario_id} open={scenario.assessment.state === "LOCKED"}>
                          <summary>
                            <div><span className={`risk-state risk-${scenario.assessment.state.toLowerCase()}`}>{scenario.assessment.state}</span><strong>{scenario.title}</strong></div>
                            <small>{scenario.event.severity} · {scenario.event.kind}</small>
                          </summary>
                          <p>{scenario.summary}</p>
                          <div className="evidence-grid">
                            <div><span>First seen</span><strong>{new Date(scenario.event.first_seen_at).toLocaleString()}</strong></div>
                            <div><span>Corroboration</span><strong>{scenario.event.corroborating_sources} source(s)</strong></div>
                            <div><span>Cross-asset</span><strong>{scenario.event.cross_asset_confirmed ? "CONFIRMED" : "PENDING"}</strong></div>
                            <div><span>Next checkpoint</span><strong>{scenario.assessment.next_checkpoint ?? "COMPLETE"}</strong></div>
                          </div>
                          <div className="reason-chips">{scenario.assessment.reasons.map((reason) => <span key={reason}>{reason}</span>)}</div>
                          <div className="manifest-row"><span>Manifest</span><code title={scenario.replay_manifest.manifest_hash}>{scenario.replay_manifest.manifest_hash.slice(0, 28)}…</code><small>{scenario.replay_manifest.entries.length} immutable entry</small></div>
                        </details>
                      ))}
                    </div>
                  </article>

                  <article className="alerts-panel">
                    <div className="panel-title"><div><p className="eyebrow">BIDIRECTIONAL FEEDBACK</p><h2>Demo alert queue</h2></div><span className="read-only">{auditBackend} AUDIT</span></div>
                    {operations.alerts.alerts.map((alert) => (
                      <div className="alert-card" key={alert.alert_id}>
                        <div className="alert-top"><span className="priority">{alert.candidate.priority}</span><strong>{alert.candidate.direction} tail risk</strong><b className={`alert-status status-${alert.status.toLowerCase()}`}>{alert.status}</b></div>
                        <p>{alert.candidate.evidence.join(" · ")}</p>
                        <dl><div><dt>Action</dt><dd>{alert.candidate.action}</dd></div><div><dt>Deduplicated</dt><dd>{alert.deduplicated_count}×</dd></div><div><dt>Rerun</dt><dd>{alert.candidate.rerun_trigger}</dd></div></dl>
                        <small>Invalidates when: {alert.candidate.invalidation_conditions.join(", ")}</small>
                        <div className="feedback-actions" aria-label="Local demo feedback">
                          <button disabled={feedbackBusy !== null} onClick={() => submitFeedback(alert.alert_id, "ACKNOWLEDGED")}>{feedbackBusy === "ACKNOWLEDGED" ? "Saving…" : "Acknowledge"}</button>
                          <button disabled={feedbackBusy !== null} onClick={() => submitFeedback(alert.alert_id, "DISMISSED")}>Dismiss</button>
                          <button disabled={feedbackBusy !== null} onClick={() => submitFeedback(alert.alert_id, "FALSE_POSITIVE")}>False positive</button>
                        </div>
                      </div>
                    ))}
                    <p className="local-disclaimer"><strong>Local audit only.</strong> Feedback is appended to the configured {auditBackend} repository and survives API restarts. It does not message anyone, change a broker account, or enable execution.</p>
                  </article>
                </div>
              </>
            )}
          </section>

          <section className="control-section" id="decision-history" aria-labelledby="history-heading">
            <div className="section-heading control-heading">
              <div><p className="eyebrow">DECISION TRACE</p><h2 id="history-heading">Session decision history</h2></div>
              <div className="ops-boundaries"><span>{serverDecisionHistory.length ? `${auditBackend} APPEND-ONLY` : "BROWSER FALLBACK"}</span><span>MAX 12 RUNS</span><span>NO ORDERS</span></div>
            </div>
            <article className="history-panel">
              <div className="history-head"><span>DATA AS OF</span><span>SCENARIO / ACTION</span><span>MODEL LINEAGE</span><span>PROVENANCE</span></div>
              {visibleDecisionHistory.length === 0 ? (
                <div className="empty-state"><strong>No decisions in this browser session</strong><span>Run one of the controlled scenarios above. This is local UI history, not a substitute for the server append-only audit.</span></div>
              ) : visibleDecisionHistory.map((item, index) => (
                <button className="history-row" key={`${item.run_id}-${index}`} onClick={() => setDecision(item)}>
                  <time>{item.data_as_of ? new Date(item.data_as_of).toLocaleTimeString() : "LOCAL"}</time>
                  <span><b className={`action-pill action-pill-${item.action.toLowerCase()}`}>{item.action}</b><small>{item.scenario.toUpperCase()}</small></span>
                  <span><strong>{item.model_version ?? "—"}</strong><small>{item.rules_version ?? "—"} · {item.code_version ?? "—"}</small></span>
                  <span><code title={item.run_id}>{item.run_id?.slice(0, 12) ?? "—"}…</code><small title={item.snapshot_id}>{item.snapshot_id?.slice(0, 18) ?? "NO SNAPSHOT"}…</small></span>
                </button>
              ))}
            </article>
          </section>

          <section className="control-section" id="attribution" aria-labelledby="attribution-heading">
            <div className="section-heading control-heading">
              <div><p className="eyebrow">POST-EVENT CONTROL PLANE</p><h2 id="attribution-heading">Attribution review &amp; alert delivery</h2></div>
              <div className="ops-boundaries"><span>SCENARIO</span><span>LOCAL</span><span>UNVERIFIED</span></div>
            </div>
            {controlPlaneError && <div className="inline-alert control-error">{controlPlaneError}</div>}
            {!controlPlane ? (
              <div className="ops-loading">Loading attribution, SSE audit and governance state…</div>
            ) : (
              <div className="control-grid">
                <article className="attribution-panel">
                  <div className="panel-title"><div><p className="eyebrow">REVERSE ATTRIBUTION</p><h2>Review queue</h2></div><span className="read-only">{controlPlane.attribution?.tasks.length ?? 0} TASKS</span></div>
                  {controlPlane.attribution?.tasks.length ? controlPlane.attribution.tasks.map((task) => (
                    <div className="task-card" key={task.task_id}>
                      <div className="task-top"><span>{task.signal.severity}</span><strong>{task.signal.kind.replaceAll("_", " ")}</strong><b>{task.review_status}</b></div>
                      <p>{task.signal.candidates[0]?.summary ?? "No causal narrative accepted; evidence review remains open."}</p>
                      <dl>
                        <div><dt>Timing</dt><dd>{task.reaction_timing_interpretation}</dd></div>
                        <div><dt>Cross-asset</dt><dd>{task.cross_asset_coherence}</dd></div>
                        <div><dt>Confidence</dt><dd>{(task.confidence * 100).toFixed(0)}%</dd></div>
                      </dl>
                      <div className="task-proof"><code title={task.signal.snapshot_id}>{task.signal.snapshot_id.slice(0, 25)}…</code><small>Counterfactual replay available · execution disabled</small></div>
                      <div className="review-actions">
                        <button disabled={reviewBusy !== null} onClick={() => reviewAttribution(task.task_id, "IN_REVIEW")}>Start review</button>
                        <button disabled={reviewBusy !== null} onClick={() => reviewAttribution(task.task_id, "INCONCLUSIVE")}>Inconclusive</button>
                        <button disabled={reviewBusy !== null} onClick={() => reviewAttribution(task.task_id, "REJECTED")}>Reject cause</button>
                      </div>
                    </div>
                  )) : <div className="empty-state"><strong>No attribution tasks</strong><span>Tasks appear only after a locally recorded major-event or abnormal-move signal. Empty is a valid state.</span></div>}
                </article>

                <article className="stream-panel">
                  <div className="panel-title"><div><p className="eyebrow">SSE DELIVERY AUDIT</p><h2>Stream status</h2></div><span className="stream-pulse"><i />POLLING</span></div>
                  <div className="stream-summary">
                    <div><span>Delivery attempts</span><strong>{controlPlane.deliveries?.deliveries.length ?? 0}</strong></div>
                    <div><span>Last event cursor</span><strong>{controlPlane.deliveries?.deliveries.at(-1)?.stream_event_id ?? "NONE"}</strong></div>
                    <div><span>Last checked</span><strong>{new Date(controlPlane.checked_at).toLocaleTimeString()}</strong></div>
                  </div>
                  <div className="delivery-list">
                    {controlPlane.deliveries?.deliveries.length ? controlPlane.deliveries.deliveries.slice(-6).reverse().map((delivery) => (
                      <div key={delivery.delivery_id}><span><i />EVENT {delivery.stream_event_id}</span><strong>{delivery.outcome}</strong><small>{new Date(delivery.attempted_at).toLocaleTimeString()} · {delivery.connection_id.slice(0, 10)}…</small></div>
                    )) : <div className="empty-state compact"><strong>No delivery attempts yet</strong><span>SSE delivery audit records attempts, not client receipt. The dashboard polls this append-only evidence every 15 seconds.</span></div>}
                  </div>
                  {controlPlane.errors.filter((error) => error.area === "deliveries").map((error) => <div className="inline-alert" key={error.area}>{error.error}</div>)}
                </article>
              </div>
            )}
          </section>

          <section className="control-section" id="governance" aria-labelledby="governance-heading">
            <div className="section-heading control-heading">
              <div><p className="eyebrow">CONTROLLED MODEL PROMOTION</p><h2 id="governance-heading">Model governance &amp; version lineage</h2></div>
              <div className="ops-boundaries"><span>FROZEN INTRADAY</span><span>EXPLICIT APPROVAL</span><span>LIVE DISABLED</span></div>
            </div>
            {!controlPlane ? <div className="ops-loading">Loading model registry…</div> : (
              <article className="governance-panel">
                <div className="governance-summary">
                  <div><span>Local champion</span><strong>{controlPlane.governance.champion?.champion.version ?? "NOT ASSIGNED"}</strong><small>{controlPlane.governance.champion?.frozen_for_session ? "SESSION FROZEN" : "Registry view"}</small></div>
                  <div><span>Calibration</span><strong>{controlPlane.governance.validation?.calibration_status ?? "UNAVAILABLE"}</strong><small>{controlPlane.governance.validation?.conclusion ?? "No automatic conclusion"}</small></div>
                  <div><span>Live eligibility</span><strong>{controlPlane.governance.validation?.live_eligible ? "ELIGIBLE" : "BLOCKED"}</strong><small>execution_enabled=false</small></div>
                  <div><span>Audit integrity</span><strong>{controlPlane.audit.integrity?.status ?? "UNAVAILABLE"}</strong><small>Schema {controlPlane.audit.integrity?.schema_version ?? "—"} · {controlPlane.audit.integrity?.foreign_key_violations ?? "—"} FK violations</small></div>
                </div>
                <div className="lineage-list">
                  {(controlPlane.governance.versions?.versions ?? []).map((version, index, versions) => (
                    <div className="lineage-row" key={version.version}>
                      <span className="lineage-node">{index === versions.length - 1 ? "●" : "○"}</span>
                      <div><strong>{version.version}</strong><small>{version.is_local_champion ? "LOCAL CHAMPION" : "CHALLENGER"} · {version.calibration_status}</small></div>
                      <dl><div><dt>Parent</dt><dd>{version.parent_version ?? "ROOT"}</dd></div><div><dt>Trained</dt><dd>{new Date(version.trained_at).toLocaleDateString()}</dd></div></dl>
                      <code title={version.artifact_hash}>{version.artifact_hash.slice(0, 24)}…</code>
                    </div>
                  ))}
                  {!controlPlane.governance.versions?.versions.length && <div className="empty-state"><strong>No model lineage available</strong><span>The governance API is unavailable or has no registered versions. Live eligibility remains blocked.</span></div>}
                </div>
                <div className="audit-replays">
                  <span>Persisted replay manifests</span><strong>{controlPlane.audit.replays?.manifests.length ?? 0}</strong><small>Latest {controlPlane.audit.replays?.manifests.at(-1)?.manifest_hash.slice(0, 28) ?? "NONE"}…</small>
                </div>
                <p className="local-disclaimer"><strong>Read-only governance view.</strong> This screen deliberately exposes no promotion or rollback shortcut. Those actions require an explicit, evidenced LOCAL/SCENARIO approval through the governed API and never enable live execution.</p>
              </article>
            )}
          </section>
        </main>
        <footer><span>MarketPilot local safety MVP</span><span>NO_TRADE is a first-class outcome · No automated execution</span></footer>
      </div>
    </div>
  );
}
