import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { Zap, GitBranch, AlertCircle } from "lucide-react";

const EXAMPLE_REPOS = [
  "https://github.com/psf/requests",
  "https://github.com/pallets/flask",
  "https://github.com/encode/httpx",
  "https://github.com/tiangolo/fastapi",
];

export function NewAnalysisPage() {
  const [repoUrl, setRepoUrl] = useState("");
  const [branch, setBranch] = useState("main");
  const [includeTests, setIncludeTests] = useState(true);
  const [includeDocs, setIncludeDocs]   = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!repoUrl.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const res = await api.post("/analyses", {
        repo_url:      repoUrl.trim(),
        branch:        branch.trim() || "main",
        include_tests: includeTests,
        include_docs:  includeDocs,
      });
      navigate(`/analyses/${res.data.analysis_id}`);
    } catch (err: any) {
      setError(err.response?.data?.detail ?? "Failed to start analysis");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="page narrow">
      <div className="page-header">
        <h1>New Analysis</h1>
      </div>

      <div className="card form-card">
        <form onSubmit={handleSubmit}>
          <div className="form-group">
            <label htmlFor="repo-url">GitHub Repository URL</label>
            <input
              id="repo-url"
              type="url"
              placeholder="https://github.com/owner/repository"
              value={repoUrl}
              onChange={e => setRepoUrl(e.target.value)}
              required
              className="input-field"
            />
          </div>

          <div className="form-row">
            <div className="form-group">
              <label htmlFor="branch">Branch</label>
              <input
                id="branch"
                type="text"
                placeholder="main"
                value={branch}
                onChange={e => setBranch(e.target.value)}
                className="input-field"
              />
            </div>
          </div>

          <div className="form-checkboxes">
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={includeTests}
                onChange={e => setIncludeTests(e.target.checked)}
              />
              Include test files in analysis
            </label>
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={includeDocs}
                onChange={e => setIncludeDocs(e.target.checked)}
              />
              Include documentation files
            </label>
          </div>

          {error && (
            <div className="error-banner">
              <AlertCircle size={15} /> {error}
            </div>
          )}

          <button
            type="submit"
            disabled={loading || !repoUrl.trim()}
            className="btn-primary submit-btn"
          >
            {loading
              ? <><span className="spinner" /> Starting analysis…</>
              : <><Zap size={15} /> Run Analysis</>
            }
          </button>
        </form>

        <div className="examples-section">
          <p className="examples-label">Try an example repository:</p>
          <div className="example-chips">
            {EXAMPLE_REPOS.map(r => (
              <button
                key={r}
                className="chip"
                onClick={() => setRepoUrl(r)}
                type="button"
              >
                <GitBranch size={12} />
                {r.replace("https://github.com/", "")}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* What happens next */}
      <div className="card info-card">
        <h3>What happens when you run an analysis</h3>
        <ol className="steps-list">
          <li><strong>Clone & Parse</strong> — Repository is cloned and parsed with tree-sitter into AST chunks at the function/class level</li>
          <li><strong>Embed & Index</strong> — Chunks embedded with CodeBERT and stored in ChromaDB for semantic retrieval</li>
          <li><strong>Bug Detection</strong> — Three layers: Semgrep/Bandit static analysis, GPT-4o semantic analysis of complex code, and anti-pattern RAG search</li>
          <li><strong>Patch Generation</strong> — Fine-tuned CodeLlama (or GPT-4o fallback) generates minimal, correct patches in unified diff format</li>
          <li><strong>Test Generation</strong> — Regression tests generated for each bug fix</li>
          <li><strong>Sandbox Evaluation</strong> — Patches applied and tests run in ephemeral Docker containers — no hallucinating results</li>
          <li><strong>Report</strong> — Full structured report with risk score, top issues, patch success rates, tracked in MLflow</li>
        </ol>
      </div>
    </div>
  );
}
