import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { GitBranch, RefreshCw } from "lucide-react";

export function AnalysesPage() {
  const [analyses, setAnalyses] = useState<any[]>([]);
  const [loading, setLoading]   = useState(true);
  const navigate = useNavigate();

  useEffect(() => {
    api.get("/analyses?limit=100")
      .then(r => setAnalyses(r.data))
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className="page">
      <div className="page-header">
        <h1>All Analyses</h1>
        <button className="btn-primary" onClick={() => navigate("/new")}>
          + New Analysis
        </button>
      </div>
      <div className="card">
        {loading ? (
          <div className="page-loading"><RefreshCw className="spin" size={18} /> Loading…</div>
        ) : analyses.length === 0 ? (
          <p className="empty-state">No analyses yet. Run your first one!</p>
        ) : (
          <table className="analyses-table">
            <thead>
              <tr>
                <th>Repository</th><th>Branch</th><th>Status</th>
                <th>Risk</th><th>Bugs</th><th>Patches</th><th>Date</th>
              </tr>
            </thead>
            <tbody>
              {analyses.map(a => (
                <tr key={a.analysis_id} className="clickable-row"
                    onClick={() => navigate(`/analyses/${a.analysis_id}`)}>
                  <td className="repo-cell">{a.repo_url?.replace("https://github.com/", "")}</td>
                  <td><code>{a.branch}</code></td>
                  <td><span className={`status-pill ${a.status}`}>{a.status}</span></td>
                  <td style={{ color: (a.risk_score ?? 0) > 0.6 ? "#ef4444" : (a.risk_score ?? 0) > 0.3 ? "#eab308" : "#22c55e" }}>
                    {Math.round((a.risk_score ?? 0) * 100)}
                  </td>
                  <td>{a.bugs_found ?? "—"}</td>
                  <td>{a.patches_passing ?? 0} / {a.patches_generated ?? 0}</td>
                  <td className="time-cell">{a.created_at ? new Date(a.created_at).toLocaleDateString() : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

export function MLflowPage() {
  const MLFLOW_URL = import.meta.env.VITE_MLFLOW_URL ?? "http://localhost:5000";
  return (
    <div className="page">
      <div className="page-header"><h1>LLMOps — MLflow</h1></div>
      <div className="card" style={{ padding: 0, overflow: "hidden", height: "80vh" }}>
        <iframe
          src={MLFLOW_URL}
          style={{ width: "100%", height: "100%", border: "none" }}
          title="MLflow"
        />
      </div>
      <p style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 8 }}>
        Tracks all prompt versions, agent runs, fine-tuning experiments, and model registry.
        Direct link: <a href={MLFLOW_URL} target="_blank" rel="noreferrer">{MLFLOW_URL}</a>
      </p>
    </div>
  );
}
