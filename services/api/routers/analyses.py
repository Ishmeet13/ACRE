"""
Analyses Router
===============
REST endpoints for creating and querying analyses.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks, Query
from pydantic import BaseModel, HttpUrl

from db import (
    create_analysis_record,
    get_analysis,
    list_analyses,
    get_bugs_for_analysis,
    get_patches_for_analysis,
)
from celery_tasks import trigger_analysis as celery_trigger
from auth import get_current_user

router = APIRouter()


# ── Request / Response models ─────────────────────────────────────────────────
class CreateAnalysisRequest(BaseModel):
    repo_url: str
    branch: str = "main"
    include_tests: bool = True
    include_docs: bool = True


class AnalysisResponse(BaseModel):
    analysis_id: str
    repo_url: str
    branch: str
    status: str
    risk_score: float
    bugs_found: int
    patches_generated: int
    patches_passing: int
    created_at: str
    completed_at: Optional[str] = None


class AnalysisSummaryResponse(BaseModel):
    analysis_id: str
    repo_url: str
    status: str
    risk_score: float
    bugs_found: int
    created_at: str


# ── Endpoints ─────────────────────────────────────────────────────────────────
@router.post("", response_model=AnalysisResponse, status_code=202)
async def create_analysis(
    req: CreateAnalysisRequest,
    # current_user: dict = Depends(get_current_user),  # uncomment in prod
):
    """
    Trigger a new analysis on a GitHub repository.
    Returns 202 Accepted immediately; poll /analyses/{id} or subscribe to WS.

    Example:
      POST /api/v1/analyses
      { "repo_url": "https://github.com/owner/repo", "branch": "main" }
    """
    analysis_id = str(uuid.uuid4())
    row = await create_analysis_record(
        analysis_id=analysis_id,
        repo_url=req.repo_url,
        branch=req.branch,
    )

    # Queue background analysis via Celery
    celery_trigger.apply_async(
        kwargs={
            "analysis_id": analysis_id,
            "repo_url": req.repo_url,
            "branch": req.branch,
            "include_tests": req.include_tests,
            "include_docs": req.include_docs,
        },
        queue="analysis",
    )

    return AnalysisResponse(**row)


@router.get("", response_model=list[AnalysisSummaryResponse])
async def list_all_analyses(
    repo_url: Optional[str] = Query(None, description="Filter by repo URL"),
    status: Optional[str]   = Query(None, description="Filter by status: running|done|error"),
    limit: int               = Query(20, ge=1, le=100),
    offset: int              = Query(0, ge=0),
):
    rows = await list_analyses(repo_url=repo_url, status=status, limit=limit, offset=offset)
    return [AnalysisSummaryResponse(**r) for r in rows]


@router.get("/{analysis_id}", response_model=AnalysisResponse)
async def get_analysis_detail(analysis_id: str):
    row = await get_analysis(analysis_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Analysis {analysis_id} not found")
    return AnalysisResponse(**row)


@router.get("/{analysis_id}/bugs")
async def get_bugs(
    analysis_id: str,
    severity: Optional[str] = None,
    bug_type: Optional[str] = None,
    file_path: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """Return bug reports for an analysis, with optional filtering."""
    row = await get_analysis(analysis_id)
    if not row:
        raise HTTPException(status_code=404, detail="Analysis not found")

    bugs = await get_bugs_for_analysis(
        analysis_id,
        severity=severity,
        bug_type=bug_type,
        file_path=file_path,
        limit=limit,
    )
    return {"total": len(bugs), "items": bugs}


@router.get("/{analysis_id}/patches")
async def get_patches(
    analysis_id: str,
    status: Optional[str] = None,
    limit: int = 50,
):
    """Return generated patches for an analysis."""
    row = await get_analysis(analysis_id)
    if not row:
        raise HTTPException(status_code=404, detail="Analysis not found")

    patches = await get_patches_for_analysis(analysis_id, status=status, limit=limit)
    return {"total": len(patches), "items": patches}


@router.get("/{analysis_id}/report")
async def get_full_report(analysis_id: str):
    """Return the compiled final report (available when status == done)."""
    from db import get_final_report
    row = await get_analysis(analysis_id)
    if not row:
        raise HTTPException(status_code=404, detail="Analysis not found")
    if row["status"] != "done":
        raise HTTPException(status_code=409, detail=f"Analysis not complete yet (status: {row['status']})")

    report = await get_final_report(analysis_id)
    return report


@router.delete("/{analysis_id}", status_code=204)
async def delete_analysis(analysis_id: str):
    """Delete an analysis and all associated data (bugs, patches, vectors)."""
    from db import delete_analysis as db_delete
    from vector_store_client import get_vector_store
    await db_delete(analysis_id)
    get_vector_store().delete_analysis(analysis_id)
