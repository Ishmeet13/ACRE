import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, AreaChart, Area, BarChart, Bar, Cell
} from "recharts";
import { api } from "../lib/api";
import { AlertTriangle, CheckCircle, GitBranch, Zap, TrendingUp, Clock } from "lucide-react";

export function OverviewPage() {
  const [analyses, setAnalyses] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const navigate = useNavigate();

  useEffect(() => {
    api.get("/analyses?limit=50")
      .then(r => setAnalyses(r.data))
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  const completed = analyses.filter(a => a.status === "done");
  const running   = analyses.filter(a => !["done","error"].includes(a.status));

  // Aggregate stats
  const totalBugs     = completed.reduce((s, a) => s + (a.bugs_found ?? 0), 0);
  const totalPatches  = completed.reduce((s, a) => s + (a.patches_generated ?? 0), 0);
  const totalPassing  = completed.reduce((s, a) => s + (a.patches_passing ?? 0), 0);
  const avgRisk       = completed.length
    ? (completed.reduce((s, a) => s + (a.risk_score ?? 0), 0) / completed.length).toFixed(2)
    : "—";

  // Risk score history (last 20 completed analyses)
  const riskHistory = [...completed]
    .slice(-20)
    .map((a, i) => ({
      i: i + 1,
      risk: Math.round((a.risk_score ?? 0) * 100),
      repo: a.repo_url?.split("/").slice(-1)[0] ?? "repo",
    }));

  // Bugs by severity across all analyses
  const severityBreakdown = [
    { name: "Critical", count: 0, color: "#ef4444" },
    { name: "High",     count: 0, color: "#f97316" },
    { name: "Medium",   count: 0, color: "#eab308" },
    { name: "Low",      count: 0, color: "#22c55e" },
  ];

  // Patch quality trend
  const qualityTrend = completed.slice(-15).map((a, i) => ({
    i: i + 1,
    rate: a.patches_generated
      ? Math.round((a.patches_passing / a.patches_generated) * 100)
      : 0,
  }));

  return (
    <div className="page">
      <div className="page-header">
        <h1>Overview</h1>
        <button className="btn-primary" onClick={() => navigate("/new")}>
          <Zap size={15} /> New Analysis
        </button>
      </div>

      {/* ── KPI Row ── */}
      <div className="kpi-row">
        <div className="kpi-card">
          <div className="kpi-icon running"><GitBranch size={18} /></div>
          <div>
            <div className="kpi-number">{analyses.length}</div>
            <div className="kpi-label">Total Analyses</div>
          </div>
        </div>
        <div className="kpi-card">
          <div className="kpi-icon danger"><AlertTriangle size={18} /></div>
          <div>
            <div className="kpi-number">{totalBugs.toLocaleString()}</div>
            <div className="kpi-label">Bugs Found</div>
          </div>
        </div>
        <div className="kpi-card">
          <div className="kpi-icon success"><CheckCircle size={18} /></div>
          <div>
            <div className="kpi-number">{totalPassing} / {totalPatches}</div>
            <div className="kpi-label">Patches Passing</div>
          </div>
        </div>
        <div className="kpi-card">
          <div className="kpi-icon warning"><TrendingUp size={18} /></div>
          <div>
            <div className="kpi-number">{avgRisk}</div>
            <div className="kpi-label">Avg Risk Score</div>
          </div>
        </div>
        {running.length > 0 && (
          <div className="kpi-card running-card">
            <div className="kpi-icon pulse"><Clock size={18} /></div>
            <div>
              <div className="kpi-number">{running.length}</div>
              <div className="kpi-label">Running Now</div>
            </div>
          </div>
        )}
      </div>

      {/* ── Charts Row ── */}
      <div className="charts-row">
        <div className="chart-card wide">
          <h4>Risk Score History <span className="chart-sub">(last 20 analyses)</span></h4>
          <ResponsiveContainer width="100%" height={200}>
            <AreaChart data={riskHistory}>
              <defs>
                <linearGradient id="riskGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%"  stopColor="#6366f1" stopOpacity={0.4}/>
                  <stop offset="95%" stopColor="#6366f1" stopOpacity={0}/>
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
              <XAxis dataKey="i" tick={{ fontSize: 11 }} />
              <YAxis domain={[0, 100]} tick={{ fontSize: 11 }} />
              <Tooltip formatter={(v: any) => [`${v}`, "Risk"]} />
              <Area type="monotone" dataKey="risk" stroke="#6366f1" fill="url(#riskGrad)" strokeWidth={2} />
            </AreaChart>
          </ResponsiveContainer>
        </div>

        <div className="chart-card">
          <h4>Patch Pass Rate <span className="chart-sub">(% per analysis)</span></h4>
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={qualityTrend}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
              <XAxis dataKey="i" tick={{ fontSize: 11 }} />
              <YAxis domain={[0, 100]} tick={{ fontSize: 11 }} />
              <Tooltip formatter={(v: any) => [`${v}%`, "Pass Rate"]} />
              <Bar dataKey="rate" radius={[3, 3, 0, 0]}>
                {qualityTrend.map((entry, i) => (
                  <Cell key={i} fill={entry.rate >= 70 ? "#22c55e" : entry.rate >= 40 ? "#eab308" : "#ef4444"} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* ── Recent Analyses ── */}
      <div className="card">
        <h3>Recent Analyses</h3>
        <table className="analyses-table">
          <thead>
            <tr>
              <th>Repository</th>
              <th>Branch</th>
              <th>Status</th>
              <th>Risk</th>
              <th>Bugs</th>
              <th>Patches</th>
              <th>Started</th>
            </tr>
          </thead>
          <tbody>
            {loading
              ? <tr><td colSpan={7} className="loading-row">Loading…</td></tr>
              : analyses.slice(0, 15).map(a => (
                  <tr key={a.analysis_id} className="clickable-row" onClick={() => navigate(`/analyses/${a.analysis_id}`)}>
                    <td className="repo-cell">
                      {a.repo_url?.replace("https://github.com/", "") ?? "—"}
                    </td>
                    <td><code>{a.branch}</code></td>
                    <td>
                      <span className={`status-pill ${a.status}`}>{a.status}</span>
                    </td>
                    <td>
                      <span style={{
                        color: (a.risk_score ?? 0) > 0.6 ? "#ef4444"
                             : (a.risk_score ?? 0) > 0.3 ? "#eab308" : "#22c55e"
                      }}>
                        {Math.round((a.risk_score ?? 0) * 100)}
                      </span>
                    </td>
                    <td>{a.bugs_found ?? "—"}</td>
                    <td>{a.patches_passing ?? 0} / {a.patches_generated ?? 0}</td>
                    <td className="time-cell">
                      {a.created_at ? new Date(a.created_at).toLocaleDateString() : "—"}
                    </td>
                  </tr>
                ))
            }
          </tbody>
        </table>
      </div>
    </div>
  );
}
