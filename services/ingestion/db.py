"""Thin DB helpers for the ingestion service (reuses the API db layer pattern)."""
from __future__ import annotations

import os
from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime, JSON, Text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

_engine = None
_session_factory = None


class Base(DeclarativeBase):
    pass


class RepoSnapshotIngestion(Base):
    __tablename__ = "repo_snapshots"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    analysis_id  = Column(String(36), index=True, nullable=False)
    repo_url     = Column(String(512))
    branch       = Column(String(128))
    s3_key       = Column(String(512))
    files_count  = Column(Integer, default=0)
    chunks_count = Column(Integer, default=0)
    created_at   = Column(DateTime, default=datetime.utcnow)


class AnalysisStatus(Base):
    """Lightweight mirror — ingestion only writes status, API owns the full record."""
    __tablename__ = "analyses"
    analysis_id = Column(String(36), primary_key=True)
    repo_url    = Column(String(512))
    branch      = Column(String(128))
    status      = Column(String(32), default="pending")
    # Extra cols exist but we don't need them here
    bugs_found           = Column(Integer, default=0)
    patches_generated    = Column(Integer, default=0)
    patches_passing      = Column(Integer, default=0)
    risk_score           = Column(Integer, default=0)
    architecture_summary = Column(Text, nullable=True)
    final_report         = Column(JSON, nullable=True)
    trigger              = Column(String(32), default="api")
    trigger_metadata     = Column(JSON, nullable=True)
    error_message        = Column(Text, nullable=True)
    created_at           = Column(DateTime, default=datetime.utcnow)
    completed_at         = Column(DateTime, nullable=True)


async def init_db(postgres_url: str):
    global _engine, _session_factory
    if not postgres_url:
        return
    async_url = postgres_url.replace("postgresql://", "postgresql+asyncpg://")
    _engine = create_async_engine(async_url, echo=False, pool_pre_ping=True)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        # Don't create_all here — API service owns schema creation
        pass


def get_db() -> AsyncSession:
    return _session_factory()


async def update_ingestion_status(analysis_id: str, status: str):
    if not _session_factory:
        return
    async with get_db() as session:
        await session.execute(
            update(AnalysisStatus)
            .where(AnalysisStatus.analysis_id == analysis_id)
            .values(status=status)
        )
        await session.commit()


async def save_repo_snapshot(
    analysis_id: str, repo_url: str, branch: str,
    files_count: int, chunks_count: int, s3_key: str,
):
    if not _session_factory:
        return
    async with get_db() as session:
        obj = RepoSnapshotIngestion(
            analysis_id=analysis_id, repo_url=repo_url, branch=branch,
            s3_key=s3_key, files_count=files_count, chunks_count=chunks_count,
        )
        session.add(obj)
        await session.commit()
