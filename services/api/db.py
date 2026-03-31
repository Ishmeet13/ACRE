"""
Database Layer (PostgreSQL / SQLAlchemy async)
=============================================
Models:
  - Analysis      — top-level record per repo scan
  - BugReport     — individual bug found
  - Patch         — generated patch for a bug
  - EvalResult    — sandbox evaluation result for a patch
  - RepoSnapshot  — S3 metadata for cloned repo archive
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Column, String, Float, Integer, Boolean, Text,
    DateTime, ForeignKey, JSON, Index, func, select, delete, update
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, relationship

# ── Engine ────────────────────────────────────────────────────────────────────
_engine = None
_session_factory = None


async def init_db(postgres_url: str):
    global _engine, _session_factory
    # Convert sync URL to async
    async_url = postgres_url.replace("postgresql://", "postgresql+asyncpg://")
    _engine = create_async_engine(async_url, echo=False, pool_size=10, max_overflow=20)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def get_db() -> AsyncSession:
    return _session_factory()


# ── Base ─────────────────────────────────────────────────────────────────────
class Base(DeclarativeBase):
    pass


# ── Models ────────────────────────────────────────────────────────────────────
class Analysis(Base):
    __tablename__ = "analyses"

    analysis_id      = Column(String(36), primary_key=True)
    repo_url         = Column(String(512), nullable=False, index=True)
    branch           = Column(String(128), default="main")
    status           = Column(String(32), default="pending", index=True)
    risk_score       = Column(Float, default=0.0)
    bugs_found       = Column(Integer, default=0)
    patches_generated = Column(Integer, default=0)
    patches_passing  = Column(Integer, default=0)
    architecture_summary = Column(Text, nullable=True)
    final_report     = Column(JSON, nullable=True)
    trigger          = Column(String(32), default="api")   # api | github_push | github_pr
    trigger_metadata = Column(JSON, nullable=True)
    error_message    = Column(Text, nullable=True)
    created_at       = Column(DateTime, default=datetime.utcnow, index=True)
    completed_at     = Column(DateTime, nullable=True)

    bugs    = relationship("BugReportModel",  back_populates="analysis", cascade="all, delete-orphan")
    patches = relationship("PatchModel",       back_populates="analysis", cascade="all, delete-orphan")
    snapshots = relationship("RepoSnapshot",  back_populates="analysis", cascade="all, delete-orphan")


class BugReportModel(Base):
    __tablename__ = "bug_reports"
    __table_args__ = (
        Index("ix_bug_analysis_severity", "analysis_id", "severity"),
        Index("ix_bug_file", "analysis_id", "file_path"),
    )

    bug_id           = Column(String(36), primary_key=True)
    analysis_id      = Column(String(36), ForeignKey("analyses.analysis_id", ondelete="CASCADE"), nullable=False, index=True)
    file_path        = Column(String(512), nullable=False)
    start_line       = Column(Integer, default=0)
    end_line         = Column(Integer, default=0)
    title            = Column(String(512))
    description      = Column(Text)
    severity         = Column(String(16), index=True)
    bug_type         = Column(String(32))
    vulnerable_code  = Column(Text)
    root_cause       = Column(Text)
    suggested_fix_description = Column(Text)
    chunk_id         = Column(String(64))
    severity_score   = Column(Float, default=0.0)
    detection_method = Column(String(32), default="llm")
    dismissed        = Column(Boolean, default=False)
    dismissed_reason = Column(Text, nullable=True)
    created_at       = Column(DateTime, default=datetime.utcnow)

    analysis = relationship("Analysis", back_populates="bugs")
    patches  = relationship("PatchModel", back_populates="bug", cascade="all, delete-orphan")


class PatchModel(Base):
    __tablename__ = "patches"

    patch_id         = Column(String(36), primary_key=True)
    bug_id           = Column(String(36), ForeignKey("bug_reports.bug_id", ondelete="CASCADE"), nullable=False, index=True)
    analysis_id      = Column(String(36), ForeignKey("analyses.analysis_id", ondelete="CASCADE"), nullable=False, index=True)
    file_path        = Column(String(512))
    original_code    = Column(Text)
    fixed_code       = Column(Text)
    unified_diff     = Column(Text)
    explanation      = Column(Text)
    confidence_score = Column(Float, default=0.5)
    risk_level       = Column(String(16), default="medium")
    additional_notes = Column(Text)
    test_hint        = Column(Text)
    model_used       = Column(String(64))
    status           = Column(String(16), default="PENDING", index=True)
    pr_url           = Column(String(512), nullable=True)
    created_at       = Column(DateTime, default=datetime.utcnow)

    analysis   = relationship("Analysis",      back_populates="patches")
    bug        = relationship("BugReportModel", back_populates="patches")
    eval_result = relationship("EvalResultModel", back_populates="patch", uselist=False, cascade="all, delete-orphan")


class EvalResultModel(Base):
    __tablename__ = "eval_results"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    patch_id      = Column(String(36), ForeignKey("patches.patch_id", ondelete="CASCADE"), nullable=False, unique=True)
    verdict       = Column(String(16))       # PASS | PARTIAL | FAIL
    tests_run     = Column(Integer, default=0)
    tests_passed  = Column(Integer, default=0)
    tests_failed  = Column(Integer, default=0)
    stdout        = Column(Text)
    stderr        = Column(Text)
    error         = Column(Text, nullable=True)
    quality_score = Column(Float, default=0.0)
    created_at    = Column(DateTime, default=datetime.utcnow)

    patch = relationship("PatchModel", back_populates="eval_result")


class RepoSnapshot(Base):
    __tablename__ = "repo_snapshots"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    analysis_id   = Column(String(36), ForeignKey("analyses.analysis_id", ondelete="CASCADE"), nullable=False)
    repo_url      = Column(String(512))
    branch        = Column(String(128))
    s3_key        = Column(String(512))
    files_count   = Column(Integer, default=0)
    chunks_count  = Column(Integer, default=0)
    created_at    = Column(DateTime, default=datetime.utcnow)

    analysis = relationship("Analysis", back_populates="snapshots")


# ── Query Helpers ─────────────────────────────────────────────────────────────
def _row_to_dict(obj) -> dict:
    d = {c.name: getattr(obj, c.name) for c in obj.__table__.columns}
    # Serialize datetimes
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


async def create_analysis_record(
    analysis_id: str, repo_url: str, branch: str = "main",
    trigger: str = "api", trigger_metadata: dict | None = None
) -> dict:
    async with get_db() as session:
        obj = Analysis(
            analysis_id=analysis_id,
            repo_url=repo_url,
            branch=branch,
            status="pending",
            trigger=trigger,
            trigger_metadata=trigger_metadata or {},
            created_at=datetime.utcnow(),
        )
        session.add(obj)
        await session.commit()
        await session.refresh(obj)
        return _row_to_dict(obj)


async def get_analysis(analysis_id: str) -> dict | None:
    async with get_db() as session:
        result = await session.execute(
            select(Analysis).where(Analysis.analysis_id == analysis_id)
        )
        obj = result.scalar_one_or_none()
        return _row_to_dict(obj) if obj else None


async def list_analyses(
    repo_url: str | None = None,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    async with get_db() as session:
        q = select(Analysis).order_by(Analysis.created_at.desc()).limit(limit).offset(offset)
        if repo_url:
            q = q.where(Analysis.repo_url == repo_url)
        if status:
            q = q.where(Analysis.status == status)
        result = await session.execute(q)
        return [_row_to_dict(obj) for obj in result.scalars().all()]


async def update_ingestion_status(analysis_id: str, status: str):
    async with get_db() as session:
        await session.execute(
            update(Analysis).where(Analysis.analysis_id == analysis_id).values(status=status)
        )
        await session.commit()


async def save_repo_snapshot(
    analysis_id: str, repo_url: str, branch: str,
    files_count: int, chunks_count: int, s3_key: str
):
    async with get_db() as session:
        obj = RepoSnapshot(
            analysis_id=analysis_id, repo_url=repo_url, branch=branch,
            s3_key=s3_key, files_count=files_count, chunks_count=chunks_count,
        )
        session.add(obj)
        await session.commit()


async def get_bugs_for_analysis(
    analysis_id: str,
    severity: str | None = None,
    bug_type: str | None = None,
    file_path: str | None = None,
    limit: int = 50,
) -> list[dict]:
    async with get_db() as session:
        q = (select(BugReportModel)
             .where(BugReportModel.analysis_id == analysis_id)
             .where(BugReportModel.dismissed == False)
             .order_by(BugReportModel.severity_score.desc())
             .limit(limit))
        if severity: q = q.where(BugReportModel.severity == severity)
        if bug_type: q = q.where(BugReportModel.bug_type == bug_type)
        if file_path: q = q.where(BugReportModel.file_path == file_path)
        result = await session.execute(q)
        return [_row_to_dict(obj) for obj in result.scalars().all()]


async def get_patches_for_analysis(
    analysis_id: str, status: str | None = None, limit: int = 50
) -> list[dict]:
    async with get_db() as session:
        q = (select(PatchModel)
             .where(PatchModel.analysis_id == analysis_id)
             .order_by(PatchModel.confidence_score.desc())
             .limit(limit))
        if status: q = q.where(PatchModel.status == status)
        result = await session.execute(q)
        return [_row_to_dict(obj) for obj in result.scalars().all()]


async def get_eval_results(patch_id: str) -> list[dict]:
    async with get_db() as session:
        result = await session.execute(
            select(EvalResultModel).where(EvalResultModel.patch_id == patch_id)
        )
        return [_row_to_dict(obj) for obj in result.scalars().all()]


async def get_final_report(analysis_id: str) -> dict | None:
    async with get_db() as session:
        result = await session.execute(
            select(Analysis.final_report).where(Analysis.analysis_id == analysis_id)
        )
        row = result.scalar_one_or_none()
        return row


async def dismiss_bug(bug_id: str, reason: str):
    async with get_db() as session:
        await session.execute(
            update(BugReportModel)
            .where(BugReportModel.bug_id == bug_id)
            .values(dismissed=True, dismissed_reason=reason)
        )
        await session.commit()


async def search_bugs(query: str, severity: str | None = None, limit: int = 20) -> list[dict]:
    async with get_db() as session:
        q = (select(BugReportModel)
             .where(func.lower(BugReportModel.title).contains(query.lower()))
             .limit(limit))
        if severity: q = q.where(BugReportModel.severity == severity)
        result = await session.execute(q)
        return [_row_to_dict(obj) for obj in result.scalars().all()]


async def get_patch(patch_id: str) -> dict | None:
    async with get_db() as session:
        result = await session.execute(select(PatchModel).where(PatchModel.patch_id == patch_id))
        obj = result.scalar_one_or_none()
        return _row_to_dict(obj) if obj else None


async def update_patch_status(patch_id: str, status: str, pr_url: str | None = None):
    async with get_db() as session:
        values = {"status": status}
        if pr_url: values["pr_url"] = pr_url
        await session.execute(
            update(PatchModel).where(PatchModel.patch_id == patch_id).values(**values)
        )
        await session.commit()


async def delete_analysis(analysis_id: str):
    async with get_db() as session:
        await session.execute(delete(Analysis).where(Analysis.analysis_id == analysis_id))
        await session.commit()


async def get_repo_stats(repo_url: str) -> dict | None:
    async with get_db() as session:
        result = await session.execute(
            select(
                func.count(Analysis.analysis_id).label("total_analyses"),
                func.avg(Analysis.risk_score).label("avg_risk_score"),
                func.sum(Analysis.bugs_found).label("total_bugs_found"),
                func.sum(Analysis.patches_generated).label("total_patches_generated"),
                func.sum(Analysis.patches_passing).label("total_patches_passing"),
            ).where(Analysis.repo_url == repo_url).where(Analysis.status == "done")
        )
        row = result.one_or_none()
        if not row or row.total_analyses == 0:
            return None
        return {
            "repo_url":               repo_url,
            "total_analyses":         row.total_analyses,
            "avg_risk_score":         round(float(row.avg_risk_score or 0), 3),
            "total_bugs_found":       row.total_bugs_found or 0,
            "total_patches_generated": row.total_patches_generated or 0,
            "total_patches_passing":  row.total_patches_passing or 0,
            "languages":              [],  # would join with bug_reports in prod
        }


async def get_successful_patches_for_finetuning(limit: int = 5000) -> list[dict]:
    """Returns PASSED patches with their original bug info — fine-tuning data source."""
    async with get_db() as session:
        result = await session.execute(
            select(PatchModel, BugReportModel)
            .join(BugReportModel, PatchModel.bug_id == BugReportModel.bug_id)
            .join(EvalResultModel, PatchModel.patch_id == EvalResultModel.patch_id)
            .where(EvalResultModel.verdict == "PASS")
            .limit(limit)
        )
        rows = []
        for patch, bug in result.all():
            rows.append({
                "bug_title":       bug.title,
                "bug_description": bug.description,
                "severity":        bug.severity,
                "bug_type":        bug.bug_type,
                "vulnerable_code": patch.original_code,
                "fixed_code":      patch.fixed_code,
                "language":        "python",   # derive from file_path in prod
                "source":          "acre",
            })
        return rows
