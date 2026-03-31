import axios from "axios";

const BASE_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export const api = axios.create({
  baseURL: `${BASE_URL}/api/v1`,
  timeout: 30_000,
  headers: { "Content-Type": "application/json" },
});

// Inject API key / JWT token from localStorage if present
api.interceptors.request.use((config) => {
  const token = localStorage.getItem("acre_token");
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

// Global error handling
api.interceptors.response.use(
  (r) => r,
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem("acre_token");
      window.location.href = "/login";
    }
    return Promise.reject(error);
  }
);

// ── GraphQL client ─────────────────────────────────────────────
export async function graphql<T = any>(
  query: string,
  variables?: Record<string, any>
): Promise<T> {
  const res = await axios.post(
    `${BASE_URL}/graphql`,
    { query, variables },
    { headers: { "Content-Type": "application/json" } }
  );
  if (res.data.errors) {
    throw new Error(res.data.errors[0].message);
  }
  return res.data.data;
}

// ── Example GraphQL queries ────────────────────────────────────
export const QUERIES = {
  GET_ANALYSIS: `
    query GetAnalysis($id: String!) {
      analysis(analysisId: $id) {
        analysisId
        repoUrl
        branch
        status
        riskScore
        bugsFound
        patchesGenerated
        architectureSummary
        bugs(limit: 100) {
          bugId title severity bugType filePath startLine description
        }
        patches(limit: 100) {
          patchId filePath explanation unifiedDiff confidenceScore modelUsed status
          evalResult { verdict testsRun testsPassed qualityScore }
        }
      }
    }
  `,

  LIST_ANALYSES: `
    query ListAnalyses($repoUrl: String, $status: String) {
      analyses(repoUrl: $repoUrl, status: $status, limit: 50) {
        analysisId repoUrl branch status riskScore bugsFound createdAt
      }
    }
  `,

  TRIGGER_ANALYSIS: `
    mutation TriggerAnalysis($input: TriggerAnalysisInput!) {
      triggerAnalysis(input: $input) {
        analysisId status createdAt
      }
    }
  `,
};
