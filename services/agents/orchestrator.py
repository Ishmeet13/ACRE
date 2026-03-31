"""
ACRE Agent Orchestrator
========================
LangGraph-based multi-agent system that drives the full reliability analysis:

  AnalysisState → [Analyzer] → [BugDetector] → [PatchGenerator] → [TestGenerator] → [Evaluator]
                                                      ↑                                    │
                                                      └────────────── retry loop ──────────┘

Each node is a specialized agent that reads from and writes to shared state.
State is persisted to Redis so the pipeline can resume after interruptions.
All runs are tracked in MLflow for full LLMOps observability.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from typing import Annotated, TypedDict, Literal

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
import mlflow

from agents.analyzer import ArchitectureAnalyzer
from agents.bug_detector import BugDetectorAgent
from agents.patch_generator import PatchGeneratorAgent
from agents.test_generator import TestGeneratorAgent
from agents.evaluator import EvaluatorAgent
from state import AnalysisState, BugReport, Patch

logger = logging.getLogger(__name__)

mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000"))


# ── LangGraph State ───────────────────────────────────────────────────────────
class GraphState(TypedDict):
    analysis_id: str
    repo_url: str
    branch: str

    # Filled by Analyzer
    architecture_map: dict          # file graph, module deps, entry points
    high_complexity_files: list[str]

    # Filled by BugDetector
    bug_reports: list[BugReport]
    static_findings: list[dict]     # from Semgrep/Bandit
    risk_score: float               # 0-1 overall repo health score

    # Filled by PatchGenerator
    patches: list[Patch]
    patch_attempts: int             # retry counter

    # Filled by TestGenerator
    test_cases: list[dict]

    # Filled by Evaluator
    eval_results: list[dict]
    final_report: dict

    # Control flow
    current_step: str
    errors: list[str]
    retry_patch_ids: list[str]      # patches that failed eval and need retry


# ── Node Definitions ──────────────────────────────────────────────────────────
async def analyze_architecture(state: GraphState) -> GraphState:
    """
    Node 1: Build a structural map of the repository.
    - Module dependency graph
    - Identifies high-complexity hotspots
    - Detects architecture patterns (MVC, microservices, monolith)
    """
    logger.info(f"[{state['analysis_id']}] Starting architecture analysis")
    agent = ArchitectureAnalyzer(analysis_id=state["analysis_id"])
    result = await agent.run()
    return {
        **state,
        "architecture_map": result.architecture_map,
        "high_complexity_files": result.high_complexity_files,
        "current_step": "bug_detection",
    }


async def detect_bugs(state: GraphState) -> GraphState:
    """
    Node 2: Multi-layer bug detection.
    Layer A — Static analysis (Semgrep + Bandit, deterministic)
    Layer B — Semantic analysis (LLM over high-complexity chunks)
    Layer C — Pattern matching (anti-pattern RAG retrieval)
    """
    logger.info(f"[{state['analysis_id']}] Starting bug detection")
    agent = BugDetectorAgent(
        analysis_id=state["analysis_id"],
        architecture_map=state["architecture_map"],
        high_complexity_files=state["high_complexity_files"],
    )
    result = await agent.run()
    return {
        **state,
        "bug_reports": result.bug_reports,
        "static_findings": result.static_findings,
        "risk_score": result.risk_score,
        "current_step": "patch_generation",
    }


async def generate_patches(state: GraphState) -> GraphState:
    """
    Node 3: Generate patches for detected bugs.
    - Uses fine-tuned CodeLlama for patch generation when available
    - Falls back to GPT-4o for complex multi-file changes
    - Produces unified diff format patches
    """
    logger.info(f"[{state['analysis_id']}] Generating patches (attempt {state.get('patch_attempts', 0) + 1})")
    agent = PatchGeneratorAgent(
        analysis_id=state["analysis_id"],
        bug_reports=state["bug_reports"],
        retry_patch_ids=state.get("retry_patch_ids", []),
    )
    result = await agent.run()
    existing_patches = state.get("patches", [])
    new_patches = [p for p in result.patches if p.patch_id not in {ep.patch_id for ep in existing_patches}]
    return {
        **state,
        "patches": existing_patches + new_patches,
        "patch_attempts": state.get("patch_attempts", 0) + 1,
        "current_step": "test_generation",
    }


async def generate_tests(state: GraphState) -> GraphState:
    """
    Node 4: Generate pytest test cases that validate the patches.
    - Regression tests for each bug fixed
    - Edge case tests based on bug type
    - Integration tests for affected modules
    """
    logger.info(f"[{state['analysis_id']}] Generating test cases")
    agent = TestGeneratorAgent(
        analysis_id=state["analysis_id"],
        patches=state["patches"],
        bug_reports=state["bug_reports"],
    )
    result = await agent.run()
    return {
        **state,
        "test_cases": result.test_cases,
        "current_step": "evaluation",
    }


async def evaluate_patches(state: GraphState) -> GraphState:
    """
    Node 5: Apply patches in ephemeral Docker sandbox, run generated tests.
    - Scores patches: PASS / FAIL / PARTIAL
    - Collects stdout, stderr, test results
    - Calculates patch quality score for MLflow tracking
    """
    logger.info(f"[{state['analysis_id']}] Evaluating patches")
    agent = EvaluatorAgent(
        analysis_id=state["analysis_id"],
        patches=state["patches"],
        test_cases=state["test_cases"],
    )
    result = await agent.run()

    failed_ids = [r["patch_id"] for r in result.eval_results if r["verdict"] == "FAIL"]

    return {
        **state,
        "eval_results": result.eval_results,
        "retry_patch_ids": failed_ids,
        "current_step": "done" if not failed_ids or state.get("patch_attempts", 0) >= 2 else "patch_generation",
    }


async def compile_final_report(state: GraphState) -> GraphState:
    """
    Node 6: Compile everything into a structured final report.
    Persists to Postgres. Publishes done event to Redis.
    """
    report = {
        "analysis_id": state["analysis_id"],
        "repo_url": state["repo_url"],
        "completed_at": datetime.utcnow().isoformat(),
        "risk_score": state.get("risk_score", 0),
        "bugs_found": len(state.get("bug_reports", [])),
        "patches_generated": len(state.get("patches", [])),
        "patches_passing": sum(
            1 for r in state.get("eval_results", []) if r.get("verdict") == "PASS"
        ),
        "architecture_summary": state.get("architecture_map", {}).get("summary", ""),
        "top_issues": [
            {
                "bug_id": b.bug_id,
                "severity": b.severity,
                "title": b.title,
                "file": b.file_path,
                "line": b.start_line,
            }
            for b in sorted(state.get("bug_reports", []), key=lambda x: x.severity_score, reverse=True)[:10]
        ],
    }
    return {**state, "final_report": report, "current_step": "done"}


# ── Routing Logic ─────────────────────────────────────────────────────────────
def route_after_eval(state: GraphState) -> Literal["generate_patches", "compile_report"]:
    """Retry patch generation up to 2 times for failed patches."""
    if state.get("retry_patch_ids") and state.get("patch_attempts", 0) < 2:
        return "generate_patches"
    return "compile_report"


# ── Graph Construction ────────────────────────────────────────────────────────
def build_graph(redis_url: str) -> StateGraph:
    checkpointer = MemorySaver()  # For production, switch to RedisSaver(redis_url)

    builder = StateGraph(GraphState)

    # Add nodes
    builder.add_node("analyze_architecture", analyze_architecture)
    builder.add_node("detect_bugs",          detect_bugs)
    builder.add_node("generate_patches",     generate_patches)
    builder.add_node("generate_tests",       generate_tests)
    builder.add_node("evaluate_patches",     evaluate_patches)
    builder.add_node("compile_report",       compile_final_report)

    # Edges
    builder.set_entry_point("analyze_architecture")
    builder.add_edge("analyze_architecture", "detect_bugs")
    builder.add_edge("detect_bugs",          "generate_patches")
    builder.add_edge("generate_patches",     "generate_tests")
    builder.add_edge("generate_tests",       "evaluate_patches")
    builder.add_conditional_edges(
        "evaluate_patches",
        route_after_eval,
        {
            "generate_patches": "generate_patches",
            "compile_report":   "compile_report",
        },
    )
    builder.add_edge("compile_report", END)

    return builder.compile(checkpointer=checkpointer)


# ── Entry Point ───────────────────────────────────────────────────────────────
async def run_analysis(analysis_id: str, repo_url: str, branch: str = "main"):
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    graph = build_graph(redis_url)

    initial_state: GraphState = {
        "analysis_id": analysis_id,
        "repo_url": repo_url,
        "branch": branch,
        "architecture_map": {},
        "high_complexity_files": [],
        "bug_reports": [],
        "static_findings": [],
        "risk_score": 0.0,
        "patches": [],
        "patch_attempts": 0,
        "test_cases": [],
        "eval_results": [],
        "final_report": {},
        "current_step": "start",
        "errors": [],
        "retry_patch_ids": [],
    }

    mlflow.set_experiment("acre_analyses")
    mlflow.end_run()  # end any stale run from a previous crash

    with mlflow.start_run(run_name=f"analysis_{analysis_id[:8]}"):
        try:
            mlflow.log_params({
                "analysis_id": analysis_id,
                "repo_url": repo_url,
                "branch": branch,
            })

            config = {"configurable": {"thread_id": analysis_id}}
            final_state = await graph.ainvoke(initial_state, config=config)

            mlflow.log_metrics({
                "bugs_found": len(final_state.get("bug_reports", [])),
                "patches_generated": len(final_state.get("patches", [])),
                "patches_passing": sum(
                    1 for r in final_state.get("eval_results", [])
                    if r.get("verdict") == "PASS"
                ),
                "risk_score": final_state.get("risk_score", 0),
                "patch_attempts": final_state.get("patch_attempts", 0),
            })

            report = final_state.get("final_report", {})
            mlflow.log_dict(report, "final_report.json")
            logger.info(f"[{analysis_id}] Analysis complete. Risk score: {report.get('risk_score')}")
            return final_state

        except Exception as e:
            mlflow.set_tag("error", str(e)[:500])
            raise