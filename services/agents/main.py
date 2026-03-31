"""
Agents Service
==============
FastAPI wrapper around the LangGraph orchestrator.
Receives analysis jobs and runs the full multi-agent pipeline.
"""
from __future__ import annotations

import asyncio
import logging
import os

from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel

from orchestrator import run_analysis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="ACRE Agents Service", version="1.0.0")
_running: set[str] = set()


class AnalyzeRequest(BaseModel):
    analysis_id: str
    repo_url: str
    branch: str = "main"


@app.post("/analyze", status_code=202)
async def analyze(req: AnalyzeRequest, background_tasks: BackgroundTasks):
    if req.analysis_id in _running:
        raise HTTPException(status_code=409, detail="Analysis already running")
    _running.add(req.analysis_id)
    background_tasks.add_task(_run_and_cleanup, req)
    return {"analysis_id": req.analysis_id, "status": "accepted"}


async def _run_and_cleanup(req: AnalyzeRequest):
    try:
        await run_analysis(req.analysis_id, req.repo_url, req.branch)
    except Exception as e:
        logger.exception(f"[{req.analysis_id}] Agent pipeline failed: {e}")
    finally:
        _running.discard(req.analysis_id)


@app.get("/health")
async def health():
    return {"ok": True, "running_analyses": len(_running)}
