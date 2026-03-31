"""
GitHub Webhook Receiver
========================
Listens for GitHub push and pull_request events and automatically
triggers ACRE analyses on new code.

Setup (in your GitHub repo → Settings → Webhooks):
  Payload URL:    https://api.acre.yourdomain.com/webhooks/github
  Content type:   application/json
  Secret:         <GITHUB_WEBHOOK_SECRET env var>
  Events:         Push, Pull requests
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Header
from celery_tasks import trigger_analysis

logger = logging.getLogger(__name__)
router = APIRouter()

import os
WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")

# Only run analysis on pushes to these branches
WATCHED_BRANCHES = {"main", "master", "develop", "staging"}

# Skip analysis if only these paths changed (docs, CI config, etc.)
SKIP_ONLY_PATHS = {".github/", "docs/", ".md", ".txt", ".yaml", ".yml"}


@router.post("/github")
async def github_webhook(
    request: Request,
    x_github_event:     str = Header(None),
    x_hub_signature_256: str = Header(None),
):
    body = await request.body()

    # ── Verify signature ──────────────────────────────────────────────────────
    if WEBHOOK_SECRET:
        expected = "sha256=" + hmac.new(
            WEBHOOK_SECRET.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, x_hub_signature_256 or ""):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = json.loads(body)
    event = x_github_event or "push"

    if event == "ping":
        return {"ok": True, "zen": payload.get("zen", "")}

    if event == "push":
        return await _handle_push(payload)

    if event == "pull_request":
        return await _handle_pull_request(payload)

    return {"ok": True, "event": event, "action": "ignored"}


async def _handle_push(payload: dict) -> dict:
    """Trigger analysis on push to watched branches."""
    ref = payload.get("ref", "")          # e.g. "refs/heads/main"
    branch = ref.removeprefix("refs/heads/")

    if branch not in WATCHED_BRANCHES:
        return {"ok": True, "action": "ignored", "reason": f"branch {branch!r} not watched"}

    # Skip if only ignored file types changed
    changed_files = [c.get("filename", "") for commit in payload.get("commits", []) for c in commit.get("added", []) + commit.get("modified", [])]
    if changed_files and all(
        any(f.startswith(skip) or f.endswith(skip) for skip in SKIP_ONLY_PATHS)
        for f in changed_files
    ):
        return {"ok": True, "action": "ignored", "reason": "only non-code files changed"}

    repo = payload.get("repository", {})
    repo_url = repo.get("clone_url", repo.get("html_url", ""))

    if not repo_url:
        raise HTTPException(status_code=400, detail="Could not determine repo URL")

    analysis_id = str(uuid.uuid4())

    # Create DB record
    from db import create_analysis_record
    await create_analysis_record(
        analysis_id=analysis_id,
        repo_url=repo_url,
        branch=branch,
        trigger="github_push",
        trigger_metadata={
            "pusher": payload.get("pusher", {}).get("name"),
            "head_commit": payload.get("head_commit", {}).get("id"),
            "compare": payload.get("compare"),
        },
    )

    # Queue analysis
    trigger_analysis.apply_async(
        kwargs={
            "analysis_id": analysis_id,
            "repo_url":    repo_url,
            "branch":      branch,
        },
        queue="analysis",
    )

    logger.info(f"Webhook push → analysis {analysis_id} queued for {repo_url}@{branch}")
    return {
        "ok":          True,
        "analysis_id": analysis_id,
        "repo_url":    repo_url,
        "branch":      branch,
    }


async def _handle_pull_request(payload: dict) -> dict:
    """Trigger analysis on PR open/synchronize against the PR branch."""
    action = payload.get("action", "")
    if action not in ("opened", "synchronize", "reopened"):
        return {"ok": True, "action": "ignored", "reason": f"PR action {action!r} not watched"}

    pr = payload.get("pull_request", {})
    head = pr.get("head", {})
    repo = head.get("repo", payload.get("repository", {}))

    repo_url = repo.get("clone_url", repo.get("html_url", ""))
    branch   = head.get("ref", "")
    pr_number = pr.get("number")

    if not repo_url or not branch:
        return {"ok": False, "reason": "missing repo or branch"}

    analysis_id = str(uuid.uuid4())

    from db import create_analysis_record
    await create_analysis_record(
        analysis_id=analysis_id,
        repo_url=repo_url,
        branch=branch,
        trigger="github_pr",
        trigger_metadata={
            "pr_number": pr_number,
            "pr_title":  pr.get("title"),
            "pr_url":    pr.get("html_url"),
            "author":    pr.get("user", {}).get("login"),
        },
    )

    trigger_analysis.apply_async(
        kwargs={
            "analysis_id": analysis_id,
            "repo_url":    repo_url,
            "branch":      branch,
        },
        queue="analysis",
    )

    logger.info(f"Webhook PR#{pr_number} → analysis {analysis_id} queued for {repo_url}@{branch}")
    return {
        "ok":          True,
        "analysis_id": analysis_id,
        "pr_number":   pr_number,
    }
