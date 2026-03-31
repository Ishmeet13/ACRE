"""
GitHub Repository Loader
========================
Clones a GitHub repository to a temp directory.
Supports private repos via token auth.
Falls back to a shallow clone for large repos.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile

logger = logging.getLogger(__name__)

MAX_REPO_SIZE_MB = int(os.getenv("MAX_REPO_SIZE_MB", "500"))


class GitHubRepoLoader:
    def __init__(self, repo_url: str, branch: str = "main", github_token: str | None = None):
        self.repo_url = repo_url
        self.branch = branch
        self.github_token = github_token

    def _auth_url(self) -> str:
        """Inject token into URL for private repo access."""
        if self.github_token and "github.com" in self.repo_url:
            return self.repo_url.replace(
                "https://github.com/",
                f"https://{self.github_token}@github.com/",
            )
        return self.repo_url

    async def clone(self) -> str:
        """Clone repo and return temp directory path."""
        tmpdir = tempfile.mkdtemp(prefix="acre_repo_")
        auth_url = self._auth_url()

        cmd = [
            "git", "clone",
            "--depth", "1",                  # shallow clone — we don't need history
            "--branch", self.branch,
            "--single-branch",
            "--filter=blob:limit=500k",      # skip large binary blobs
            auth_url,
            tmpdir,
        ]

        logger.info(f"Cloning {self.repo_url}@{self.branch}")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

        if proc.returncode != 0:
            err = stderr.decode()
            # Try without --filter if server doesn't support partial clone
            if "filter" in err.lower() or "not supported" in err.lower():
                logger.warning("Partial clone not supported, retrying without filter")
                cmd_no_filter = [c for c in cmd if not c.startswith("--filter")]
                proc2 = await asyncio.create_subprocess_exec(*cmd_no_filter,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                _, err2 = await asyncio.wait_for(proc2.communicate(), timeout=300)
                if proc2.returncode != 0:
                    raise RuntimeError(f"Git clone failed: {err2.decode()[:500]}")
            else:
                raise RuntimeError(f"Git clone failed: {err[:500]}")

        logger.info(f"Cloned to {tmpdir}")
        return tmpdir
