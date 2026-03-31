"""
ACRE Ingestion Service
======================
Clones GitHub repositories, parses them with tree-sitter for structural
understanding, chunks intelligently, and stores vectors in ChromaDB.
Also uploads repo snapshots to S3 for audit trails.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from github_loader import GitHubRepoLoader
from ast_parser import ASTParser
from vector_store import CodeVectorStore
from s3_client import S3Client
from db import init_db, save_repo_snapshot, update_ingestion_status

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="ACRE Ingestion Service", version="1.0.0")

# ── Clients ───────────────────────────────────────────────────────────────────
redis_client: aioredis.Redis = None
vector_store: CodeVectorStore = None
s3_client: S3Client = None

SUPPORTED_EXTENSIONS = {
    ".py", ".js", ".ts", ".java", ".go", ".rs", ".cpp", ".c",
    ".cs", ".rb", ".php", ".swift", ".kt", ".scala"
}
MAX_FILE_SIZE_BYTES = 500_000  # 500 KB — skip generated/minified files


@app.on_event("startup")
async def startup():
    global redis_client, vector_store, s3_client

    redis_client = aioredis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    vector_store = CodeVectorStore(
        host=os.getenv("CHROMA_HOST", "localhost"),
        port=int(os.getenv("CHROMA_PORT", "8000")),
    )
    s3_client = S3Client(bucket=os.getenv("S3_BUCKET", "acre-artifacts"))
    await init_db(os.getenv("POSTGRES_URL"))
    logger.info("Ingestion service started")


# ── Request / Response Models ─────────────────────────────────────────────────
class IngestRequest(BaseModel):
    repo_url: str           # e.g. "https://github.com/owner/repo"
    branch: str = "main"
    analysis_id: str        # UUID issued by API gateway
    include_tests: bool = True
    include_docs: bool = True


class IngestStatus(BaseModel):
    analysis_id: str
    status: str             # pending | cloning | parsing | vectorizing | uploading | done | error
    files_processed: int = 0
    chunks_indexed: int = 0
    error: str | None = None


# ── Routes ────────────────────────────────────────────────────────────────────
@app.post("/ingest", status_code=202)
async def ingest_repo(req: IngestRequest, background_tasks: BackgroundTasks):
    """
    Kicks off async repo ingestion. Returns immediately; progress tracked via
    Redis key  acre:ingestion:{analysis_id}  and published to channel
    acre:events:{analysis_id} for WebSocket relay.
    """
    status_key = f"acre:ingestion:{req.analysis_id}"
    await redis_client.hset(status_key, mapping={"status": "pending", "files_processed": 0})
    await redis_client.expire(status_key, 3600 * 24)

    background_tasks.add_task(_run_ingestion, req)
    return {"analysis_id": req.analysis_id, "status": "accepted"}


@app.get("/status/{analysis_id}", response_model=IngestStatus)
async def get_status(analysis_id: str):
    key = f"acre:ingestion:{analysis_id}"
    data = await redis_client.hgetall(key)
    if not data:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return IngestStatus(analysis_id=analysis_id, **{k.decode(): v.decode() for k, v in data.items()})


@app.get("/health")
async def health():
    return {"ok": True}


# ── Core Ingestion Pipeline ───────────────────────────────────────────────────
async def _run_ingestion(req: IngestRequest):
    """
    Full pipeline:
      1. Clone repo  →  tempdir
      2. Walk files, filter, AST-parse
      3. Chunk intelligently (function/class level)
      4. Embed + store in ChromaDB
      5. Upload snapshot to S3
      6. Publish done event
    """
    analysis_id = req.analysis_id
    status_key = f"acre:ingestion:{analysis_id}"

    async def _update(status: str, **kwargs):
        mapping = {"status": status, **{k: str(v) for k, v in kwargs.items()}}
        await redis_client.hset(status_key, mapping=mapping)
        await redis_client.publish(
            f"acre:events:{analysis_id}",
            json.dumps({"event": "ingestion_progress", "status": status, **kwargs}),
        )

    tmpdir = None
    try:
        # 1. Clone ─────────────────────────────────────────────────────────────
        await _update("cloning")
        loader = GitHubRepoLoader(
            repo_url=req.repo_url,
            branch=req.branch,
            github_token=os.getenv("GITHUB_TOKEN"),
        )
        tmpdir = await loader.clone()
        logger.info(f"[{analysis_id}] Cloned {req.repo_url} → {tmpdir}")

        # 2. Walk + filter files ───────────────────────────────────────────────
        await _update("parsing")
        parser = ASTParser()
        code_files = _collect_files(tmpdir, req.include_tests, req.include_docs)
        logger.info(f"[{analysis_id}] Found {len(code_files)} files to parse")

        # 3. Parse + chunk ─────────────────────────────────────────────────────
        all_chunks: list[dict] = []
        for fpath in code_files:
            try:
                chunks = parser.parse_file(fpath, repo_root=tmpdir)
                all_chunks.extend(chunks)
            except Exception as e:
                logger.warning(f"[{analysis_id}] Could not parse {fpath}: {e}")

        await _update("parsing", files_processed=len(code_files), chunks_ready=len(all_chunks))
        logger.info(f"[{analysis_id}] Produced {len(all_chunks)} chunks")

        # 4. Embed + index ─────────────────────────────────────────────────────
        await _update("vectorizing")
        indexed = await vector_store.upsert_chunks(
            analysis_id=analysis_id,
            repo_url=req.repo_url,
            chunks=all_chunks,
        )
        await _update("vectorizing", chunks_indexed=indexed)

        # 5. Upload snapshot to S3 ─────────────────────────────────────────────
        await _update("uploading")
        snapshot_key = f"snapshots/{analysis_id}.tar.gz"
        try:
            await s3_client.upload_directory(tmpdir, snapshot_key)
        except Exception as e:
            logger.warning(f"[{analysis_id}] S3 upload skipped (no credentials): {e}")
            snapshot_key = f"local://{analysis_id}"
        # 6. Persist metadata to Postgres ─────────────────────────────────────
        await save_repo_snapshot(
            analysis_id=analysis_id,
            repo_url=req.repo_url,
            branch=req.branch,
            files_count=len(code_files),
            chunks_count=indexed,
            s3_key=snapshot_key,
        )
        await update_ingestion_status(analysis_id, "done")

        await _update("done", files_processed=len(code_files), chunks_indexed=indexed)
        logger.info(f"[{analysis_id}] Ingestion complete")

    except Exception as e:
        logger.exception(f"[{analysis_id}] Ingestion failed: {e}")
        await _update("error", error=str(e))
    finally:
        if tmpdir and Path(tmpdir).exists():
            shutil.rmtree(tmpdir, ignore_errors=True)


def _collect_files(root: str, include_tests: bool, include_docs: bool) -> list[str]:
    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next"}
    if not include_tests:
        skip_dirs.update({"tests", "test", "spec", "__tests__"})

    collected = []
    for fpath in Path(root).rglob("*"):
        if any(part in skip_dirs for part in fpath.parts):
            continue
        if fpath.suffix not in SUPPORTED_EXTENSIONS:
            continue
        if not include_docs and "doc" in fpath.parts:
            continue
        if fpath.stat().st_size > MAX_FILE_SIZE_BYTES:
            continue
        collected.append(str(fpath))
    return collected
