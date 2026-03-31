import { useState } from "react";
import { BrowserRouter, Routes, Route, NavLink } from "react-router-dom";
import { AnalysesPage, MLflowPage } from "./pages/OtherPages";
import { AnalysisDetailPage } from "./pages/AnalysisDetailPage";
import { NewAnalysisPage } from "./pages/NewAnalysisPage";
import { OverviewPage } from "./pages/OverviewPage";
import { useWebSocket } from "./hooks/useWebSocket";
import {
  Activity, GitBranch, Zap, BarChart2, PlusCircle, Bell, Settings
} from "lucide-react";
import "./styles/globals.css";

export default function App() {
  const [notifications, setNotifications] = useState<string[]>([]);
  const [sidebarOpen, setSidebarOpen] = useState(true);

  return (
    <BrowserRouter>
      <div className="app-shell">
        {/* ── Sidebar ── */}
        <aside className={`sidebar ${sidebarOpen ? "open" : "collapsed"}`}>
          <div className="sidebar-brand">
            <div className="brand-icon">
              <Zap size={20} />
            </div>
            {sidebarOpen && (
              <div className="brand-text">
                <span className="brand-name">ACRE</span>
                <span className="brand-sub">Reliability Engineer</span>
              </div>
            )}
          </div>

          <nav className="sidebar-nav">
            <NavLink to="/" end className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}>
              <Activity size={18} />
              {sidebarOpen && <span>Overview</span>}
            </NavLink>
            <NavLink to="/analyses" className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}>
              <GitBranch size={18} />
              {sidebarOpen && <span>Analyses</span>}
            </NavLink>
            <NavLink to="/new" className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}>
              <PlusCircle size={18} />
              {sidebarOpen && <span>New Analysis</span>}
            </NavLink>
            <NavLink to="/mlflow" className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}>
              <BarChart2 size={18} />
              {sidebarOpen && <span>LLMOps</span>}
            </NavLink>
          </nav>

          <button className="sidebar-toggle" onClick={() => setSidebarOpen(o => !o)}>
            {sidebarOpen ? "←" : "→"}
          </button>
        </aside>

        {/* ── Main ── */}
        <main className="main-content">
          <Routes>
            <Route path="/"              element={<OverviewPage />} />
            <Route path="/analyses"      element={<AnalysesPage />} />
            <Route path="/analyses/:id"  element={<AnalysisDetailPage />} />
            <Route path="/new"           element={<NewAnalysisPage />} />
            <Route path="/mlflow"        element={<MLflowPage />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  );
}
