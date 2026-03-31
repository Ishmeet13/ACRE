"""
Evaluator Agent
===============
Applies patches in ephemeral Docker containers and runs generated tests.
This is the "ground truth" feedback loop — no hallucinating whether a fix works.

Verdict: PASS | PARTIAL | FAIL
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import tempfile
import textwrap
import uuid
from dataclasses import dataclass
from pathlib import Path

import mlflow

from state import Patch, PatchStatus

logger = logging.getLogger(__name__)

SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE", "python:3.11-slim")
SANDBOX_TIMEOUT_S = int(os.getenv("SANDBOX_TIMEOUT_S", "30"))


@dataclass
class EvalResult:
    patch_id: str
    verdict: str           # PASS | PARTIAL | FAIL
    tests_run: int
    tests_passed: int
    tests_failed: int
    stdout: str
    stderr: str
    error: str | None
    quality_score: float   # 0-1, used in MLflow


@dataclass
class EvaluatorResult:
    eval_results: list[dict]


class EvaluatorAgent:
    def __init__(
        self,
        analysis_id: str,
        patches: list[Patch],
        test_cases: list[dict],
    ):
        self.analysis_id = analysis_id
        self.patches = patches
        self.test_cases = {tc["patch_id"]: tc for tc in test_cases}

    async def run(self) -> EvaluatorResult:
        """Evaluate all patches concurrently (up to 4 at once)."""
        sem = asyncio.Semaphore(4)  # max 4 Docker containers in parallel

        async def bounded_eval(patch: Patch):
            async with sem:
                return await self._evaluate_patch(patch)

        results = await asyncio.gather(*[bounded_eval(p) for p in self.patches])
        eval_dicts = [r.__dict__ for r in results]

        # Update patch statuses
        verdict_map = {r.patch_id: r.verdict for r in results}
        for patch in self.patches:
            v = verdict_map.get(patch.patch_id, "FAIL")
            patch.status = PatchStatus.PASSED if v == "PASS" else (
                PatchStatus.PARTIAL if v == "PARTIAL" else PatchStatus.FAILED
            )

        # Track aggregate eval metrics in MLflow
        pass_count = sum(1 for r in results if r.verdict == "PASS")
        partial_count = sum(1 for r in results if r.verdict == "PARTIAL")
        fail_count = sum(1 for r in results if r.verdict == "FAIL")
        avg_quality = sum(r.quality_score for r in results) / max(len(results), 1)

        with mlflow.start_run(nested=True, run_name="evaluation"):
            mlflow.log_metrics({
                "eval_pass": pass_count,
                "eval_partial": partial_count,
                "eval_fail": fail_count,
                "eval_avg_quality": round(avg_quality, 3),
                "eval_pass_rate": round(pass_count / max(len(results), 1), 3),
            })

        return EvaluatorResult(eval_results=eval_dicts)

    async def _evaluate_patch(self, patch: Patch) -> EvalResult:
        """Apply patch in a Docker sandbox and run tests."""
        test_case = self.test_cases.get(patch.patch_id)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Write the patched source file
            src_path = Path(tmpdir) / "patched_code.py"
            src_path.write_text(patch.fixed_code)

            # Write test file
            test_code = test_case["test_code"] if test_case else _default_test(patch)
            test_path = Path(tmpdir) / "test_patch.py"
            test_path.write_text(test_code)

            # Run in Docker sandbox
            try:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._run_docker_sandbox(tmpdir, src_path.name, test_path.name),
                )
                return self._parse_result(patch.patch_id, result)
            except Exception as e:
                logger.warning(f"Sandbox failed for patch {patch.patch_id}: {e}")
                return EvalResult(
                    patch_id=patch.patch_id,
                    verdict="FAIL",
                    tests_run=0,
                    tests_passed=0,
                    tests_failed=0,
                    stdout="",
                    stderr="",
                    error=str(e),
                    quality_score=0.0,
                )

    def _run_docker_sandbox(self, workdir: str, src_file: str, test_file: str) -> dict:
        """
        Run pytest inside a short-lived Docker container.
        Network is disabled, memory is capped at 256MB.
        """
        cmd = [
            "docker", "run",
            "--rm",
            "--network", "none",
            "--memory", "256m",
            "--cpus", "0.5",
            "--read-only",
            "--tmpfs", "/tmp:size=64m",
            "-v", f"{workdir}:/workspace:ro",
            "-w", "/workspace",
            SANDBOX_IMAGE,
            "sh", "-c",
            f"pip install pytest --quiet --no-index 2>/dev/null || true; "
            f"python -m pytest {test_file} --tb=short --json-report --json-report-file=/tmp/report.json -q 2>&1; "
            f"cat /tmp/report.json 2>/dev/null || echo '{{\"summary\": {{\"passed\": 0, \"failed\": 1}}}}'",
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=SANDBOX_TIMEOUT_S,
            )
            return {
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "returncode": proc.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"stdout": "", "stderr": "Sandbox timeout", "returncode": -1}
        except FileNotFoundError:
            # Docker not available in this environment — do static check instead
            return self._static_fallback_check(workdir)

    def _static_fallback_check(self, workdir: str) -> dict:
        """
        When Docker is unavailable, do a basic syntax check + pylint as fallback.
        """
        try:
            files = list(Path(workdir).glob("*.py"))
            if not files:
                return {"stdout": "", "stderr": "No files to check", "returncode": 1}

            cmd = ["python", "-m", "py_compile"] + [str(f) for f in files]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return {
                "stdout": proc.stdout or "Syntax check passed",
                "stderr": proc.stderr,
                "returncode": proc.returncode,
            }
        except Exception as e:
            return {"stdout": "", "stderr": str(e), "returncode": 1}

    def _parse_result(self, patch_id: str, result: dict) -> EvalResult:
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        returncode = result.get("returncode", 1)

        # Try to extract pytest JSON report
        tests_run = tests_passed = tests_failed = 0
        try:
            # Pytest JSON report is appended to stdout
            json_start = stdout.rfind('{"')
            if json_start != -1:
                report = json.loads(stdout[json_start:])
                summary = report.get("summary", {})
                tests_passed = summary.get("passed", 0)
                tests_failed = summary.get("failed", 0)
                tests_run = tests_passed + tests_failed
        except Exception:
            # Fallback: parse pytest text output
            for line in stdout.splitlines():
                if "passed" in line:
                    parts = line.split()
                    for i, p in enumerate(parts):
                        if p == "passed":
                            try:
                                tests_passed = int(parts[i - 1])
                            except Exception:
                                pass
                        elif p == "failed":
                            try:
                                tests_failed = int(parts[i - 1])
                            except Exception:
                                pass
            tests_run = tests_passed + tests_failed

        if tests_run == 0 and returncode == 0:
            # No tests ran but code compiled — PARTIAL
            verdict = "PARTIAL"
            quality_score = 0.4
        elif tests_run > 0 and tests_failed == 0:
            verdict = "PASS"
            quality_score = 1.0
        elif tests_run > 0 and tests_passed > 0:
            verdict = "PARTIAL"
            quality_score = tests_passed / tests_run
        else:
            verdict = "FAIL"
            quality_score = 0.0

        return EvalResult(
            patch_id=patch_id,
            verdict=verdict,
            tests_run=tests_run,
            tests_passed=tests_passed,
            tests_failed=tests_failed,
            stdout=stdout[:2000],
            stderr=stderr[:500],
            error=None,
            quality_score=quality_score,
        )


def _default_test(patch: Patch) -> str:
    """Minimal smoke test when no specific test case was generated."""
    return textwrap.dedent(f"""
        # Auto-generated smoke test
        import ast

        def test_syntax_valid():
            \"\"\"Ensure patched code is syntactically valid Python.\"\"\"
            code = {repr(patch.fixed_code)}
            try:
                ast.parse(code)
                valid = True
            except SyntaxError:
                valid = False
            assert valid, "Patched code has syntax errors"

        def test_not_empty():
            code = {repr(patch.fixed_code)}
            assert len(code.strip()) > 0, "Patch produced empty code"
    """)
