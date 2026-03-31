"""
GraphQL Schema (Strawberry)
============================
Exposes all ACRE data through a typed GraphQL API.
Supports deep querying:
  - repo → analyses → bugs → patches → eval results
  - filtering by severity, file, status
  - real-time subscription hooks (via polling resolver)
"""
from __future__ import annotations

import strawberry
from strawberry.scalars import JSON
from typing import Optional, List
import uuid
from datetime import datetime

from db import (
    get_analysis,
    list_analyses,
    get_bugs_for_analysis,
    get_patches_for_analysis,
    get_eval_results,
    create_analysis_record,
)
from celery_tasks import trigger_analysis


# ── Types ─────────────────────────────────────────────────────────────────────
@strawberry.type
class BugType:
    bug_id: str
    file_path: str
    start_line: int
    end_line: int
    title: str
    description: str
    severity: str
    bug_type: str
    vulnerable_code: str
    root_cause: str
    suggested_fix_description: str
    detection_method: str
    severity_score: float


@strawberry.type
class EvalResultType:
    verdict: str          # PASS | PARTIAL | FAIL
    tests_run: int
    tests_passed: int
    tests_failed: int
    quality_score: float
    stdout: str
    stderr: str


@strawberry.type
class PatchType:
    patch_id: str
    bug_id: str
    file_path: str
    explanation: str
    unified_diff: str
    confidence_score: float
    risk_level: str
    model_used: str
    status: str
    eval_result: Optional[EvalResultType]


@strawberry.type
class AnalysisType:
    analysis_id: str
    repo_url: str
    branch: str
    status: str
    risk_score: float
    bugs_found: int
    patches_generated: int
    patches_passing: int
    created_at: str
    completed_at: Optional[str]

    @strawberry.field
    async def bugs(
        self,
        severity: Optional[str] = None,
        bug_type: Optional[str] = None,
        file_path: Optional[str] = None,
        limit: int = 50,
    ) -> List[BugType]:
        rows = await get_bugs_for_analysis(
            self.analysis_id,
            severity=severity,
            bug_type=bug_type,
            file_path=file_path,
            limit=limit,
        )
        return [BugType(**r) for r in rows]

    @strawberry.field
    async def patches(
        self,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[PatchType]:
        rows = await get_patches_for_analysis(
            self.analysis_id,
            status=status,
            limit=limit,
        )
        patches = []
        for r in rows:
            eval_rows = await get_eval_results(r["patch_id"])
            eval_result = EvalResultType(**eval_rows[0]) if eval_rows else None
            patches.append(PatchType(**r, eval_result=eval_result))
        return patches

    @strawberry.field
    async def architecture_summary(self) -> Optional[str]:
        """Returns the architecture map summary from the analysis."""
        row = await get_analysis(self.analysis_id)
        return row.get("architecture_summary") if row else None


@strawberry.type
class RepoStatsType:
    repo_url: str
    total_analyses: int
    avg_risk_score: float
    total_bugs_found: int
    total_patches_generated: int
    total_patches_passing: int
    languages: List[str]


# ── Input Types ───────────────────────────────────────────────────────────────
@strawberry.input
class TriggerAnalysisInput:
    repo_url: str
    branch: str = "main"
    include_tests: bool = True
    include_docs: bool = True


# ── Query ─────────────────────────────────────────────────────────────────────
@strawberry.type
class Query:

    @strawberry.field
    async def analysis(self, analysis_id: str) -> Optional[AnalysisType]:
        row = await get_analysis(analysis_id)
        return AnalysisType(**row) if row else None

    @strawberry.field
    async def analyses(
        self,
        repo_url: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[AnalysisType]:
        rows = await list_analyses(repo_url=repo_url, status=status, limit=limit, offset=offset)
        return [AnalysisType(**r) for r in rows]

    @strawberry.field
    async def repo_stats(self, repo_url: str) -> Optional[RepoStatsType]:
        from db import get_repo_stats
        stats = await get_repo_stats(repo_url)
        return RepoStatsType(**stats) if stats else None

    @strawberry.field
    async def search_bugs(
        self,
        query: str,
        severity: Optional[str] = None,
        limit: int = 20,
    ) -> List[BugType]:
        """Full-text search across bug titles and descriptions."""
        from db import search_bugs
        rows = await search_bugs(query=query, severity=severity, limit=limit)
        return [BugType(**r) for r in rows]


# ── Mutation ──────────────────────────────────────────────────────────────────
@strawberry.type
class Mutation:

    @strawberry.mutation
    async def trigger_analysis(self, input: TriggerAnalysisInput) -> AnalysisType:
        """Kick off a new analysis for a GitHub repository."""
        analysis_id = str(uuid.uuid4())
        row = await create_analysis_record(
            analysis_id=analysis_id,
            repo_url=input.repo_url,
            branch=input.branch,
        )
        # Queue Celery task
        trigger_analysis.delay(
            analysis_id=analysis_id,
            repo_url=input.repo_url,
            branch=input.branch,
            include_tests=input.include_tests,
            include_docs=input.include_docs,
        )
        return AnalysisType(**row)

    @strawberry.mutation
    async def apply_patch(self, patch_id: str) -> PatchType:
        """Mark a patch as accepted and create a GitHub PR."""
        from github_pr import create_pull_request
        from db import get_patch, update_patch_status
        patch = await get_patch(patch_id)
        if not patch:
            raise ValueError(f"Patch {patch_id} not found")
        pr_url = await create_pull_request(patch)
        await update_patch_status(patch_id, "APPLIED", pr_url=pr_url)
        patch["status"] = "APPLIED"
        return PatchType(**patch, eval_result=None)

    @strawberry.mutation
    async def dismiss_bug(self, bug_id: str, reason: str) -> bool:
        """Mark a bug as dismissed (false positive or won't fix)."""
        from db import dismiss_bug
        await dismiss_bug(bug_id, reason)
        return True
