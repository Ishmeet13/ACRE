"""
MLflow LLMOps Utilities
========================
Helpers for tracking:
  - Prompt versions (with full text + parameters)
  - Agent run lineage
  - Retrieval quality (chunk scores, coverage)
  - Patch success rates over model versions
  - Fine-tuned model versions in registry

Usage:
  from mlflow_config import track_agent_run, PromptRegistry

  with track_agent_run("bug_detection", analysis_id="...") as run:
      run.log_prompt("system_prompt", SYSTEM_PROMPT)
      run.log_retrieval(chunks, query)
      ... do work ...
      run.log_outcome(bugs_found=5, quality_score=0.8)
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generator, Optional

import mlflow
from mlflow.entities import RunStatus

MLFLOW_TRACKING_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000")
mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)


# ── Prompt Registry ───────────────────────────────────────────────────────────
class PromptRegistry:
    """
    Version-controlled prompt storage in MLflow.
    Each unique prompt text gets a deterministic hash as its version.
    """
    _cache: dict[str, str] = {}  # hash → version string

    @staticmethod
    def register(name: str, prompt_text: str, tags: dict | None = None) -> str:
        """Store a prompt and return its content hash (version ID)."""
        content_hash = hashlib.sha256(prompt_text.encode()).hexdigest()[:12]
        version = f"{name}:{content_hash}"

        if version not in PromptRegistry._cache:
            # Log prompt as an MLflow artifact
            with mlflow.start_run(run_name=f"prompt_{name}_{content_hash}",
                                  experiment_id=_get_or_create_experiment("acre_prompts")):
                mlflow.log_text(prompt_text, f"prompts/{name}.txt")
                mlflow.set_tags({
                    "prompt_name": name,
                    "prompt_hash": content_hash,
                    "prompt_version": version,
                    **(tags or {}),
                })
            PromptRegistry._cache[version] = content_hash

        return version

    @staticmethod
    def diff(v1: str, v2: str) -> str:
        """Return a diff between two prompt versions (fetched from MLflow)."""
        import difflib
        p1 = PromptRegistry._fetch(v1)
        p2 = PromptRegistry._fetch(v2)
        return "\n".join(difflib.unified_diff(p1.splitlines(), p2.splitlines(), lineterm=""))

    @staticmethod
    def _fetch(version: str) -> str:
        # In a real implementation, fetch from MLflow artifact store
        return f"[Prompt version: {version}]"


# ── Agent Run Tracker ─────────────────────────────────────────────────────────
@dataclass
class AgentRunContext:
    """Context object for tracking a single agent node execution."""
    run_name: str
    analysis_id: str
    _start_time: float = field(default_factory=time.time)
    _params: dict = field(default_factory=dict)
    _metrics: dict = field(default_factory=dict)
    _artifacts: list[tuple[str, str]] = field(default_factory=list)

    def log_prompt(self, name: str, text: str, model: str = "gpt-4o"):
        """Track the prompt text and which model it was sent to."""
        version = PromptRegistry.register(name, text)
        self._params[f"prompt_{name}_version"] = version
        self._params[f"prompt_{name}_model"] = model
        self._params[f"prompt_{name}_chars"] = len(text)

    def log_retrieval(self, chunks: list[dict], query: str):
        """Track retrieval quality metrics."""
        if not chunks:
            return
        scores = [c.get("score", 0) for c in chunks]
        self._metrics["retrieval_chunks"] = len(chunks)
        self._metrics["retrieval_avg_score"] = round(sum(scores) / len(scores), 4)
        self._metrics["retrieval_max_score"] = round(max(scores), 4)
        self._params["retrieval_query_chars"] = len(query)

    def log_outcome(self, **metrics):
        """Log final outcome metrics for this agent node."""
        self._metrics.update(metrics)

    def log_artifact(self, content: str, filename: str):
        self._artifacts.append((content, filename))


@contextmanager
def track_agent_run(
    node_name: str,
    analysis_id: str,
    parent_run_id: str | None = None,
) -> Generator[AgentRunContext, None, None]:
    """
    Context manager for tracking a single agent node execution.

    Usage:
        with track_agent_run("bug_detection", analysis_id="abc123") as ctx:
            ctx.log_prompt("system", SYSTEM_PROMPT)
            # ... do work ...
            ctx.log_outcome(bugs_found=5)
    """
    ctx = AgentRunContext(run_name=node_name, analysis_id=analysis_id)
    exp_id = _get_or_create_experiment("acre_agent_runs")

    with mlflow.start_run(
        run_name=f"{node_name}_{analysis_id[:8]}",
        experiment_id=exp_id,
        nested=parent_run_id is not None,
        tags={
            "analysis_id": analysis_id,
            "node": node_name,
        },
    ) as run:
        try:
            yield ctx
            status = RunStatus.FINISHED
        except Exception as e:
            ctx._metrics["error"] = str(e)[:200]
            status = RunStatus.FAILED
            raise
        finally:
            # Flush all collected params/metrics/artifacts
            elapsed = time.time() - ctx._start_time
            ctx._params["analysis_id"] = analysis_id
            ctx._metrics["wall_time_s"] = round(elapsed, 2)

            if ctx._params:
                mlflow.log_params(ctx._params)
            if ctx._metrics:
                mlflow.log_metrics(ctx._metrics)
            for content, fname in ctx._artifacts:
                mlflow.log_text(content, fname)


# ── Patch Success Rate Tracker ────────────────────────────────────────────────
def log_patch_success_rates(model_version: str, eval_results: list[dict]):
    """
    Log patch success rates for a specific model version.
    Used to compare fine-tuned model vs GPT-4o over time.
    """
    exp_id = _get_or_create_experiment("acre_patch_quality")
    total = len(eval_results)
    if total == 0:
        return

    passed  = sum(1 for r in eval_results if r.get("verdict") == "PASS")
    partial = sum(1 for r in eval_results if r.get("verdict") == "PARTIAL")
    failed  = sum(1 for r in eval_results if r.get("verdict") == "FAIL")
    avg_q   = sum(r.get("quality_score", 0) for r in eval_results) / total

    with mlflow.start_run(run_name=f"patch_eval_{model_version}", experiment_id=exp_id):
        mlflow.log_params({"model_version": model_version})
        mlflow.log_metrics({
            "pass_count":    passed,
            "partial_count": partial,
            "fail_count":    failed,
            "pass_rate":     round(passed / total, 4),
            "avg_quality":   round(avg_q, 4),
            "total_patches": total,
        })


# ── Model Registry Helpers ────────────────────────────────────────────────────
def promote_model_to_production(model_name: str, version: int):
    """
    Promote a fine-tuned model version to Production stage in MLflow registry.
    Called after a model passes quality thresholds.
    """
    client = mlflow.tracking.MlflowClient()
    client.transition_model_version_stage(
        name=model_name,
        version=str(version),
        stage="Production",
        archive_existing_versions=True,
    )
    print(f"Model {model_name} v{version} promoted to Production")


def get_production_model_uri(model_name: str = "acre-codellama-lora") -> str | None:
    """Get the URI of the current Production model for inference."""
    client = mlflow.tracking.MlflowClient()
    try:
        versions = client.get_latest_versions(model_name, stages=["Production"])
        if versions:
            return f"models:/{model_name}/Production"
    except Exception:
        pass
    return None


# ── Utilities ─────────────────────────────────────────────────────────────────
def _get_or_create_experiment(name: str) -> str:
    exp = mlflow.get_experiment_by_name(name)
    if exp is None:
        return mlflow.create_experiment(name)
    return exp.experiment_id
