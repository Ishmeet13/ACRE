"""
Celery Tasks
============
Background task queue for async repo analysis.
Workers run in separate pods in Kubernetes.
"""
from __future__ import annotations

import asyncio
import logging
import os

from celery import Celery

logger = logging.getLogger(__name__)

BROKER_URL   = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/1")
BACKEND_URL  = os.getenv("REDIS_URL",          "redis://localhost:6379/1")

celery_app = Celery(
    "acre",
    broker=BROKER_URL,
    backend=BACKEND_URL,
)

celery_app.conf.update(
    task_serializer      = "json",
    accept_content       = ["json"],
    result_serializer    = "json",
    timezone             = "UTC",
    task_track_started   = True,
    task_acks_late       = True,
    worker_prefetch_multiplier = 1,  # one task at a time per worker for long jobs
    task_routes = {
        "celery_tasks.trigger_analysis":    {"queue": "analysis"},
        "celery_tasks.run_finetuning_job":  {"queue": "finetuning"},
    },
)


@celery_app.task(
    bind=True,
    name="celery_tasks.trigger_analysis",
    max_retries=2,
    default_retry_delay=30,
    soft_time_limit=1800,   # 30 min soft limit
    time_limit=2100,         # 35 min hard limit
)
def trigger_analysis(
    self,
    analysis_id: str,
    repo_url: str,
    branch: str = "main",
    include_tests: bool = True,
    include_docs: bool = True,
):
    """
    Full ACRE pipeline:
      1. Call ingestion service to clone + vectorize
      2. Call agent orchestrator to run LangGraph pipeline
      3. Update final status in DB
    """
    import httpx
    import time

    ingestion_url = os.getenv("INGESTION_SERVICE_URL", "http://ingestion:8080")
    agent_url     = os.getenv("AGENT_SERVICE_URL",     "http://agents:8080")

    try:
        # Step 1: Trigger ingestion (async call, poll until done)
        logger.info(f"[{analysis_id}] Triggering ingestion")
        resp = httpx.post(f"{ingestion_url}/ingest", json={
            "repo_url":    repo_url,
            "branch":      branch,
            "analysis_id": analysis_id,
            "include_tests": include_tests,
            "include_docs":  include_docs,
        }, timeout=30)
        resp.raise_for_status()

        # Poll ingestion status (max 20 min)
        for _ in range(240):  # 240 * 5s = 20 min
            status_resp = httpx.get(f"{ingestion_url}/status/{analysis_id}", timeout=30)
            status = status_resp.json().get("status", "")
            if status == "done":
                break
            if status == "error":
                raise RuntimeError(f"Ingestion failed: {status_resp.json().get('error')}")
            time.sleep(5)
        else:
            raise TimeoutError("Ingestion timed out after 20 minutes")

        # Step 2: Run agent pipeline
        logger.info(f"[{analysis_id}] Triggering agent pipeline")
        agent_resp = httpx.post(f"{agent_url}/analyze", json={
            "analysis_id": analysis_id,
            "repo_url":    repo_url,
            "branch":      branch,
        }, timeout=1200)  # 20 min timeout for agent run
        agent_resp.raise_for_status()

        logger.info(f"[{analysis_id}] Analysis complete")
        return {"analysis_id": analysis_id, "status": "done"}

    except Exception as exc:
        logger.exception(f"[{analysis_id}] Task failed: {exc}")
        # Update DB with error status
        try:
            import httpx as _httpx
            api_url = os.getenv("API_INTERNAL_URL", "http://localhost:8000")
            _httpx.patch(
                f"{api_url}/internal/analyses/{analysis_id}/error",
                json={"error": str(exc)},
                timeout=5,
            )
        except Exception:
            pass
        raise self.retry(exc=exc)


@celery_app.task(
    name="celery_tasks.run_finetuning_job",
    max_retries=1,
    soft_time_limit=14400,  # 4 hours
)
def run_finetuning_job(
    data_dir: str = "./training_data",
    epochs: int = 3,
    batch_size: int = 4,
):
    """Trigger a PEFT/LoRA fine-tuning run. Runs on GPU node via K8s Job."""
    from finetuning.train import collect_bug_fix_pairs, train, evaluate_model, OUTPUT_DIR
    import mlflow

    pairs = collect_bug_fix_pairs(data_dir=data_dir)
    split = int(len(pairs) * 0.9)
    train(pairs[:split], num_epochs=epochs, batch_size=batch_size)
    metrics = evaluate_model(OUTPUT_DIR, pairs[split:])

    # Promote model if quality threshold met
    if metrics.get("pass_rate", 0) >= 0.70:
        from mlflow_config import promote_model_to_production
        client = mlflow.tracking.MlflowClient()
        versions = client.get_latest_versions("acre-codellama-lora", stages=["Staging"])
        if versions:
            promote_model_to_production("acre-codellama-lora", int(versions[-1].version))

    return metrics
