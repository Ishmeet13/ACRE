import { useState, useEffect } from "react";
import { useParams } from "react-router-dom";
import {
  RadarChart, Radar, PolarGrid, PolarAngleAxis,
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer,
  Cell, PieChart, Pie, Legend, LineChart, Line, CartesianGrid,
} from "recharts";
import { useWebSocket } from "../hooks/useWebSocket";
import { api } from "../lib/api";
import {
  AlertTriangle, CheckCircle, Clock, Code, GitPullRequest,
  Terminal, ChevronDown, ChevronRight, Shield, Cpu, RefreshCw
} from "lucide-react";

const SEVERITY_COLOR: Record<string, string> = {
  CRITICAL: "#ef4444",
  HIGH:     "#f97316",
  MEDIUM:   "#eab308",
  LOW:      "#22c55e",
  INFO:     "#6b7280",
};

const VERDICT_COLOR: Record<string, string> = {
  PASS:    "#22c55e",
  PARTIAL: "#eab308",
  FAIL:    "#ef4444",
};

// ── Agent Pipeline Steps ─────────────────────────────────────────────────────
const PIPELINE_STEPS = [
  { key: "cloning",       label: "Clone Repo",        icon: <GitPullRequest size={14} /> },
  { key: "parsing",       label: "AST Parse",          icon: <Code size={14} /> },
  { key: "vectorizing",   label: "Embed & Index",      icon: <Cpu size={14} /> },
  { key: "bug_detection", label: "Detect Bugs",        icon: <AlertTriangle size={14} /> },
  { key: "patch_gen",     label: "Generate Patches",   icon: <Terminal size={14} /> },
  { key: "test_gen",      label: "Generate Tests",     icon: <CheckCircle size={14} /> },
  { key: "evaluation",    label: "Evaluate Patches",   icon: <Shield size={14} /> },
  { key: "done",          label: "Complete",           icon: <CheckCircle size={14} /> },
];

function PipelineProgress({ currentStep }: { currentStep: string }) {
  const currentIdx = PIPELINE_STEPS.findIndex(s => s.key === currentStep);
  return (
    <div className="pipeline-bar">
      {PIPELINE_STEPS.map((step, i) => {
        const state = i < currentIdx ? "done" : i === currentIdx ? "active" : "pending";
        return (
          <div key={step.key} className={`pipeline-step ${state}`}>
            <div className="step-icon">{step.icon}</div>
            <span className="step-label">{step.label}</span>
            {i < PIPELINE_STEPS.length - 1 && (
              <div className={`step-connector ${state === "done" ? "done" : ""}`} />
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── Bug Row ──────────────────────────────────────────────────────────────────
function BugRow({ bug }: { bug: any }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="bug-row">
      <div className="bug-header" onClick={() => setOpen(o => !o)}>
        <span className="severity-badge" style={{ background: SEVERITY_COLOR[bug.severity] }}>
          {bug.severity}
        </span>
        <span className="bug-title">{bug.title}</span>
        <span className="bug-file">{bug.file_path}:{bug.start_line}</span>
        <span className="bug-type-tag">{bug.bug_type}</span>
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
      </div>
      {open && (
        <div className="bug-detail">
          <p className="bug-description">{bug.description}</p>
          <div className="bug-sections">
            <div>
              <h5>Root Cause</h5>
              <p>{bug.root_cause}</p>
            </div>
            <div>
              <h5>Suggested Fix</h5>
              <p>{bug.suggested_fix_description}</p>
            </div>
          </div>
          <pre className="code-block"><code>{bug.vulnerable_code}</code></pre>
        </div>
      )}
    </div>
  );
}

// ── Patch Row ────────────────────────────────────────────────────────────────
function PatchRow({ patch }: { patch: any }) {
  const [open, setOpen] = useState(false);
  const verdict = patch.eval_result?.verdict ?? patch.status;
  return (
    <div className="patch-row">
      <div className="patch-header" onClick={() => setOpen(o => !o)}>
        <span
          className="verdict-badge"
          style={{ background: VERDICT_COLOR[verdict] ?? "#6b7280" }}
        >
          {verdict}
        </span>
        <span className="patch-file">{patch.file_path}</span>
        <span className="confidence">
          {Math.round((patch.confidence_score ?? 0) * 100)}% confident
        </span>
        <span className="model-tag">{patch.model_used}</span>
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
      </div>
      {open && (
        <div className="patch-detail">
          <p className="patch-explanation">{patch.explanation}</p>
          {patch.eval_result && (
            <div className="eval-summary">
              <span>Tests: {patch.eval_result.tests_passed}/{patch.eval_result.tests_run} passing</span>
              <span>Quality: {Math.round(patch.eval_result.quality_score * 100)}%</span>
            </div>
          )}
          <pre className="diff-block"><code>{patch.unified_diff}</code></pre>
          {patch.eval_result?.stdout && (
            <details>
              <summary>Test Output</summary>
              <pre className="output-block">{patch.eval_result.stdout}</pre>
            </details>
          )}
        </div>
      )}
    </div>
  );
}

// ── Main Page ────────────────────────────────────────────────────────────────
export function AnalysisDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [analysis, setAnalysis] = useState<any>(null);
  const [bugs, setBugs] = useState<any[]>([]);
  const [patches, setPatches] = useState<any[]>([]);
  const [activeTab, setActiveTab] = useState<"bugs" | "patches" | "charts">("bugs");
  const [loading, setLoading] = useState(true);
  const [liveStatus, setLiveStatus] = useState<string>("pending");
  const [liveStats, setLiveStats] = useState<Record<string, number>>({});

  // WebSocket for real-time agent progress
  const { lastMessage } = useWebSocket(`${import.meta.env.VITE_WS_URL}/ws/${id}`);

  useEffect(() => {
    if (!lastMessage) return;
    try {
      const data = JSON.parse(lastMessage);
      if (data.status) setLiveStatus(data.status);
      if (data.files_processed) setLiveStats(prev => ({
        ...prev, files_processed: Number(data.files_processed)
      }));
      if (data.chunks_indexed) setLiveStats(prev => ({
        ...prev, chunks_indexed: Number(data.chunks_indexed)
      }));
      // Refresh data when analysis completes
      if (data.status === "done") {
        loadData();
      }
    } catch {}
  }, [lastMessage]);

  async function loadData() {
    if (!id) return;
    setLoading(true);
    try {
      const [a, b, p] = await Promise.all([
        api.get(`/analyses/${id}`),
        api.get(`/analyses/${id}/bugs?limit=100`),
        api.get(`/analyses/${id}/patches?limit=100`),
      ]);
      setAnalysis(a.data);
      setBugs(b.data.items ?? []);
      setPatches(p.data.items ?? []);
      setLiveStatus(a.data.status);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { loadData(); }, [id]);

  // ── Chart data ──────────────────────────────────────────────
  const severityData = Object.entries(
    bugs.reduce((acc: any, b: any) => {
      acc[b.severity] = (acc[b.severity] || 0) + 1;
      return acc;
    }, {})
  ).map(([name, value]) => ({ name, value }));

  const verdictData = Object.entries(
    patches.reduce((acc: any, p: any) => {
      const v = p.eval_result?.verdict ?? p.status;
      acc[v] = (acc[v] || 0) + 1;
      return acc;
    }, {})
  ).map(([name, value]) => ({ name, value }));

  // Bug type breakdown for radar chart
  const bugTypeData = [
    { type: "Security",     count: bugs.filter(b => b.bug_type === "security").length },
    { type: "Logic",        count: bugs.filter(b => b.bug_type === "logic").length },
    { type: "Reliability",  count: bugs.filter(b => b.bug_type === "reliability").length },
    { type: "Performance",  count: bugs.filter(b => b.bug_type === "performance").length },
    { type: "Code Smell",   count: bugs.filter(b => b.bug_type === "code_smell").length },
  ];

  const isRunning = ["cloning", "parsing", "vectorizing", "bug_detection",
                     "patch_gen", "test_gen", "evaluation"].includes(liveStatus);

  if (loading) {
    return (
      <div className="page-loading">
        <RefreshCw className="spin" size={24} />
        <span>Loading analysis...</span>
      </div>
    );
  }

  return (
    <div className="detail-page">
      {/* ── Header ── */}
      <div className="detail-header">
        <div>
          <h1 className="repo-title">{analysis?.repo_url?.replace("https://github.com/", "")}</h1>
          <span className="branch-tag">⎇ {analysis?.branch}</span>
        </div>
        <div className="header-stats">
          <div className={`status-pill ${liveStatus}`}>
            {isRunning && <span className="pulse-dot" />}
            {liveStatus}
          </div>
          <div className="risk-score">
            <span>Risk Score</span>
            <span
              className="risk-value"
              style={{ color: (analysis?.risk_score ?? 0) > 0.6 ? "#ef4444" : (analysis?.risk_score ?? 0) > 0.3 ? "#eab308" : "#22c55e" }}
            >
              {Math.round((analysis?.risk_score ?? 0) * 100)}
            </span>
          </div>
        </div>
      </div>

      {/* ── Pipeline Progress ── */}
      {isRunning && (
        <div className="card pipeline-card">
          <h3>Agent Pipeline</h3>
          <PipelineProgress currentStep={liveStatus} />
          {liveStats.files_processed && (
            <div className="live-stats">
              <span>Files processed: {liveStats.files_processed}</span>
              <span>Chunks indexed: {liveStats.chunks_indexed ?? "…"}</span>
            </div>
          )}
        </div>
      )}

      {/* ── Summary Cards ── */}
      <div className="summary-grid">
        <div className="summary-card danger">
          <AlertTriangle size={20} />
          <div>
            <span className="card-number">{bugs.filter(b => b.severity === "CRITICAL" || b.severity === "HIGH").length}</span>
            <span className="card-label">High-Severity Bugs</span>
          </div>
        </div>
        <div className="summary-card">
          <Shield size={20} />
          <div>
            <span className="card-number">{bugs.length}</span>
            <span className="card-label">Total Issues Found</span>
          </div>
        </div>
        <div className="summary-card success">
          <CheckCircle size={20} />
          <div>
            <span className="card-number">{patches.filter(p => (p.eval_result?.verdict ?? p.status) === "PASS").length}</span>
            <span className="card-label">Passing Patches</span>
          </div>
        </div>
        <div className="summary-card">
          <Code size={20} />
          <div>
            <span className="card-number">{patches.length}</span>
            <span className="card-label">Patches Generated</span>
          </div>
        </div>
      </div>

      {/* ── Tabs ── */}
      <div className="tabs">
        {(["bugs", "patches", "charts"] as const).map(tab => (
          <button
            key={tab}
            className={`tab-btn ${activeTab === tab ? "active" : ""}`}
            onClick={() => setActiveTab(tab)}
          >
            {tab.charAt(0).toUpperCase() + tab.slice(1)}
            {tab === "bugs"    && <span className="tab-count">{bugs.length}</span>}
            {tab === "patches" && <span className="tab-count">{patches.length}</span>}
          </button>
        ))}
      </div>

      {/* ── Tab Content ── */}
      {activeTab === "bugs" && (
        <div className="bug-list">
          {bugs.length === 0
            ? <p className="empty-state">No bugs found {isRunning ? "(analysis in progress…)" : ""}</p>
            : bugs.map(b => <BugRow key={b.bug_id} bug={b} />)
          }
        </div>
      )}

      {activeTab === "patches" && (
        <div className="patch-list">
          {patches.length === 0
            ? <p className="empty-state">No patches yet {isRunning ? "(generating…)" : ""}</p>
            : patches.map(p => <PatchRow key={p.patch_id} patch={p} />)
          }
        </div>
      )}

      {activeTab === "charts" && (
        <div className="charts-grid">
          <div className="chart-card">
            <h4>Bug Severity Distribution</h4>
            <ResponsiveContainer width="100%" height={220}>
              <PieChart>
                <Pie data={severityData} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={80} label>
                  {severityData.map(entry => (
                    <Cell key={entry.name} fill={SEVERITY_COLOR[entry.name] ?? "#6b7280"} />
                  ))}
                </Pie>
                <Tooltip />
                <Legend />
              </PieChart>
            </ResponsiveContainer>
          </div>

          <div className="chart-card">
            <h4>Patch Evaluation Results</h4>
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={verdictData}>
                <XAxis dataKey="name" />
                <YAxis />
                <Tooltip />
                <Bar dataKey="value">
                  {verdictData.map(entry => (
                    <Cell key={entry.name} fill={VERDICT_COLOR[entry.name] ?? "#6b7280"} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>

          <div className="chart-card">
            <h4>Bug Type Radar</h4>
            <ResponsiveContainer width="100%" height={220}>
              <RadarChart data={bugTypeData}>
                <PolarGrid />
                <PolarAngleAxis dataKey="type" />
                <Radar name="Bugs" dataKey="count" stroke="#6366f1" fill="#6366f1" fillOpacity={0.35} />
              </RadarChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}
    </div>
  );
}
