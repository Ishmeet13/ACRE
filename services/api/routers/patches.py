"""Patches router — get, update and apply individual patches."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from db import get_patch, update_patch_status, dismiss_bug

router = APIRouter()


class DismissRequest(BaseModel):
    reason: str = "false_positive"


@router.get("/{patch_id}")
async def get_patch_detail(patch_id: str):
    patch = await get_patch(patch_id)
    if not patch:
        raise HTTPException(status_code=404, detail="Patch not found")
    return patch


@router.post("/{patch_id}/accept")
async def accept_patch(patch_id: str):
    """Mark patch as accepted. In production this opens a GitHub PR."""
    patch = await get_patch(patch_id)
    if not patch:
        raise HTTPException(status_code=404, detail="Patch not found")
    await update_patch_status(patch_id, "ACCEPTED")
    return {"patch_id": patch_id, "status": "ACCEPTED"}


@router.post("/{patch_id}/reject")
async def reject_patch(patch_id: str, body: DismissRequest):
    patch = await get_patch(patch_id)
    if not patch:
        raise HTTPException(status_code=404, detail="Patch not found")
    await update_patch_status(patch_id, "REJECTED")
    if patch.get("bug_id"):
        await dismiss_bug(patch["bug_id"], body.reason)
    return {"patch_id": patch_id, "status": "REJECTED"}
