"""Repos router — aggregate stats per GitHub repository URL."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from db import list_analyses, get_repo_stats

router = APIRouter()


@router.get("")
async def list_repos():
    """Return all unique repos that have been analysed."""
    rows = await list_analyses(limit=500)
    seen = {}
    for r in rows:
        url = r["repo_url"]
        if url not in seen:
            seen[url] = {"repo_url": url, "analyses": 0, "last_status": r["status"]}
        seen[url]["analyses"] += 1
        seen[url]["last_status"] = r["status"]
    return list(seen.values())


@router.get("/{repo_id}/stats")
async def repo_stats(repo_id: str):
    import urllib.parse
    repo_url = urllib.parse.unquote(repo_id)
    stats = await get_repo_stats(repo_url)
    if not stats:
        raise HTTPException(status_code=404, detail="No analyses found for this repo")
    return stats
