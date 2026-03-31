"""
Bug Detector Agent
==================
Three-layer detection:
  1. Static: Semgrep (OWASP rules) + Bandit (Python security)
  2. Semantic: LLM analysis of high-complexity chunks retrieved from ChromaDB
  3. Pattern: RAG over known anti-pattern database
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Any

import mlflow
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.output_parsers import JsonOutputParser
from pydantic import BaseModel, Field

from state import BugReport, BugSeverity
from vector_store_client import get_vector_store

logger = logging.getLogger(__name__)

SEVERITY_WEIGHTS = {
    BugSeverity.CRITICAL: 1.0,
    BugSeverity.HIGH: 0.7,
    BugSeverity.MEDIUM: 0.4,
    BugSeverity.LOW: 0.1,
    BugSeverity.INFO: 0.05,
}

# Semgrep rule sets to run
SEMGREP_RULESETS = [
    "p/owasp-top-ten",
    "p/python",
    "p/javascript",
    "p/security-audit",
    "p/r2c-bug-scan",
]


@dataclass
class BugDetectionResult:
    bug_reports: list[BugReport]
    static_findings: list[dict]
    risk_score: float


class BugDetectorAgent:
    def __init__(
        self,
        analysis_id: str,
        architecture_map: dict,
        high_complexity_files: list[str],
    ):
        self.analysis_id = analysis_id
        self.architecture_map = architecture_map
        self.high_complexity_files = high_complexity_files
        self.llm = ChatOpenAI(
            model="gpt-4o",
            temperature=0,
            api_key=os.getenv("OPENAI_API_KEY"),
        )
        self.vector_store = get_vector_store()

    async def run(self) -> BugDetectionResult:
        with mlflow.start_run(nested=True, run_name="bug_detection"):
            # Run all three layers concurrently
            static_task = asyncio.create_task(self._run_static_analysis())
            semantic_task = asyncio.create_task(self._run_semantic_analysis())
            pattern_task = asyncio.create_task(self._run_pattern_detection())

            static_findings, semantic_bugs, pattern_bugs = await asyncio.gather(
                static_task, semantic_task, pattern_task
            )

            all_bugs = self._deduplicate(semantic_bugs + pattern_bugs)
            # Merge static findings into bug reports where overlapping
            all_bugs = self._merge_static(all_bugs, static_findings)

            risk_score = self._calculate_risk_score(all_bugs)

            mlflow.log_metrics({
                "static_findings": len(static_findings),
                "semantic_bugs": len(semantic_bugs),
                "pattern_bugs": len(pattern_bugs),
                "total_bugs": len(all_bugs),
                "risk_score": risk_score,
            })

            return BugDetectionResult(
                bug_reports=all_bugs,
                static_findings=static_findings,
                risk_score=risk_score,
            )

    # ── Layer 1: Static Analysis ──────────────────────────────────────────────
    async def _run_static_analysis(self) -> list[dict]:
        """Run Semgrep and Bandit in parallel in a subprocess."""
        findings = []

        semgrep_findings = await asyncio.get_event_loop().run_in_executor(
            None, self._run_semgrep
        )
        findings.extend(semgrep_findings)

        bandit_findings = await asyncio.get_event_loop().run_in_executor(
            None, self._run_bandit
        )
        findings.extend(bandit_findings)

        logger.info(f"[{self.analysis_id}] Static analysis: {len(findings)} findings")
        return findings

    def _run_semgrep(self) -> list[dict]:
        """Run semgrep with OWASP and security rulesets."""
        try:
            # In production, repo is checked out to a known path
            # For demo, we query the vector store for file paths
            files = self.vector_store.list_files(self.analysis_id)
            if not files:
                return []

            cmd = [
                "semgrep",
                "--json",
                "--config", "p/owasp-top-ten",
                "--config", "p/python",
                "--config", "p/security-audit",
                "--max-target-bytes", "500000",
                "--timeout", "30",
            ] + files[:50]  # cap for demo

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode not in (0, 1):
                logger.warning(f"Semgrep error: {result.stderr[:200]}")
                return []

            data = json.loads(result.stdout)
            return [
                {
                    "tool": "semgrep",
                    "rule_id": r.get("check_id", ""),
                    "file_path": r.get("path", ""),
                    "start_line": r.get("start", {}).get("line", 0),
                    "end_line": r.get("end", {}).get("line", 0),
                    "message": r.get("extra", {}).get("message", ""),
                    "severity": r.get("extra", {}).get("severity", "WARNING"),
                    "code_snippet": r.get("extra", {}).get("lines", ""),
                }
                for r in data.get("results", [])
            ]
        except Exception as e:
            logger.warning(f"Semgrep failed: {e}")
            return []

    def _run_bandit(self) -> list[dict]:
        """Run Bandit for Python security analysis."""
        try:
            python_files = [
                f for f in self.vector_store.list_files(self.analysis_id)
                if f.endswith(".py")
            ]
            if not python_files:
                return []

            cmd = ["bandit", "-f", "json", "-l", "-i", "-r"] + python_files[:30]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            data = json.loads(result.stdout or '{"results": []}')

            return [
                {
                    "tool": "bandit",
                    "rule_id": r.get("test_id", ""),
                    "file_path": r.get("filename", ""),
                    "start_line": r.get("line_number", 0),
                    "end_line": r.get("line_number", 0),
                    "message": r.get("issue_text", ""),
                    "severity": r.get("issue_severity", "LOW"),
                    "code_snippet": r.get("code", ""),
                }
                for r in data.get("results", [])
            ]
        except Exception as e:
            logger.warning(f"Bandit failed: {e}")
            return []

    # ── Layer 2: Semantic LLM Analysis ────────────────────────────────────────
    async def _run_semantic_analysis(self) -> list[BugReport]:
        """
        For each high-complexity file, retrieve chunks and ask GPT-4o
        to reason about bugs, security issues, and risky patterns.
        """
        bugs: list[BugReport] = []
        targets = self.high_complexity_files[:15]  # process top 15 complex files

        system_prompt = """You are an expert code reviewer and security engineer.
Analyze the provided code chunk and identify:
1. Bugs (null pointer dereferences, off-by-one errors, incorrect logic)
2. Security vulnerabilities (injection, auth bypass, data exposure)
3. Reliability risks (race conditions, resource leaks, unhandled exceptions)
4. Code smells that indicate latent bugs

Return a JSON array of findings. Each finding must have:
{
  "title": "short description",
  "description": "detailed explanation of the bug and its impact",
  "severity": "CRITICAL|HIGH|MEDIUM|LOW|INFO",
  "bug_type": "security|logic|reliability|performance|code_smell",
  "start_line": <int>,
  "end_line": <int>,
  "vulnerable_code": "the exact snippet that is problematic",
  "root_cause": "why this is a bug",
  "suggested_fix_description": "high-level fix approach"
}

Return ONLY the JSON array, no other text."""

        for file_path in targets:
            chunks = self.vector_store.query_by_file(self.analysis_id, file_path)
            if not chunks:
                continue

            # Focus on complex chunks
            complex_chunks = sorted(
                chunks,
                key=lambda c: c.get("complexity_score", 0),
                reverse=True
            )[:5]

            for chunk in complex_chunks:
                try:
                    messages = [
                        SystemMessage(content=system_prompt),
                        HumanMessage(content=f"Analyze this code chunk:\n\n```{chunk.get('language', '')}\n{chunk.get('document', '')}\n```"),
                    ]

                    # with mlflow.start_run(nested=True, run_name=f"llm_analysis_{chunk['chunk_id'][:8]}"):
                    #     mlflow.log_param("chunk_id", chunk["chunk_id"])
                    #     mlflow.log_param("file_path", file_path)

                    #     response = await self.llm.ainvoke(messages)
                    #     raw = response.content.strip()

                    #     # Log prompt + response in MLflow
                    #     mlflow.log_text(str(messages[1].content), "prompt.txt")
                    #     mlflow.log_text(raw, "response.txt")
                    response = await self.llm.ainvoke(messages)
                    raw = response.content.strip()
                    if "```json" in raw:
                        raw = raw.split("```json")[1].split("```")[0].strip()
                    elif "```" in raw:
                        raw = raw.split("```")[1].split("```")[0].strip()
                    # Handle empty response
                    if not raw or raw == "[]":
                        continue
                    findings = json.loads(raw)
                    for f in findings:
                        bugs.append(BugReport(
                            bug_id=str(uuid.uuid4()),
                            analysis_id=self.analysis_id,
                            file_path=file_path,
                            start_line=int(f.get("start_line", 0)),
                            end_line=int(f.get("end_line", 0)),
                            title=f.get("title", ""),
                            description=f.get("description", ""),
                            severity=BugSeverity(f.get("severity", "MEDIUM")),
                            bug_type=f.get("bug_type", "logic"),
                            vulnerable_code=f.get("vulnerable_code", ""),
                            root_cause=f.get("root_cause", ""),
                            suggested_fix_description=f.get("suggested_fix_description", ""),
                            chunk_id=chunk["chunk_id"],
                            severity_score=SEVERITY_WEIGHTS.get(
                                BugSeverity(f.get("severity", "MEDIUM")), 0.4
                            ),
                        ))
                except Exception as e:
                    logger.warning(f"LLM analysis failed for chunk {chunk.get('chunk_id')}: {e}")

        logger.info(f"[{self.analysis_id}] Semantic analysis: {len(bugs)} bugs")
        return bugs

    # ── Layer 3: Pattern Detection via RAG ────────────────────────────────────
    async def _run_pattern_detection(self) -> list[BugReport]:
        """
        Query the codebase for known anti-patterns using semantic search.
        Queries like 'eval user input', 'sql concatenation', 'exec shell command'.
        """
        ANTI_PATTERNS = [
            ("SQL injection via string concat", "sql string concatenation format query"),
            ("Command injection", "subprocess shell=True exec eval user input"),
            ("Hardcoded secrets", "password secret key hardcoded string literal"),
            ("Race condition", "global variable thread shared state no lock"),
            ("Unchecked return value", "error ignore return value exception swallow"),
            ("Integer overflow", "integer arithmetic unchecked overflow"),
            ("Path traversal", "file path join user input directory traversal"),
        ]

        bugs: list[BugReport] = []
        for pattern_name, query in ANTI_PATTERNS:
            results = self.vector_store.query(
                self.analysis_id, query, n_results=3
            )
            for r in results:
                if r["score"] < 0.7:  # only strong matches
                    continue
                bugs.append(BugReport(
                    bug_id=str(uuid.uuid4()),
                    analysis_id=self.analysis_id,
                    file_path=r.get("file_path", ""),
                    start_line=r.get("start_line", 0),
                    end_line=r.get("end_line", 0),
                    title=f"Potential {pattern_name}",
                    description=f"Code pattern matches known anti-pattern: {pattern_name}",
                    severity=BugSeverity.HIGH,
                    bug_type="security",
                    vulnerable_code=r.get("document", "")[:500],
                    root_cause=pattern_name,
                    suggested_fix_description="Review and sanitize inputs",
                    chunk_id=r.get("chunk_id", ""),
                    severity_score=0.7,
                    detection_method="pattern_rag",
                ))

        return bugs

    # ── Utilities ─────────────────────────────────────────────────────────────
    def _deduplicate(self, bugs: list[BugReport]) -> list[BugReport]:
        """Remove duplicate bugs (same file + approximate line range)."""
        seen = set()
        unique = []
        for bug in bugs:
            key = (bug.file_path, bug.start_line // 5, bug.severity)
            if key not in seen:
                seen.add(key)
                unique.append(bug)
        return unique

    def _merge_static(
        self, bugs: list[BugReport], static_findings: list[dict]
    ) -> list[BugReport]:
        """Add static findings that weren't caught by LLM layers."""
        static_bug_keys = {(f["file_path"], f["start_line"]) for f in static_findings}
        existing_keys = {(b.file_path, b.start_line) for b in bugs}
        new_from_static = []
        for f in static_findings:
            k = (f["file_path"], f["start_line"])
            if k not in existing_keys:
                new_from_static.append(BugReport(
                    bug_id=str(uuid.uuid4()),
                    analysis_id=self.analysis_id,
                    file_path=f["file_path"],
                    start_line=f["start_line"],
                    end_line=f["end_line"],
                    title=f["message"][:100],
                    description=f["message"],
                    severity=_map_static_severity(f["severity"]),
                    bug_type="security",
                    vulnerable_code=f.get("code_snippet", ""),
                    root_cause=f.get("rule_id", ""),
                    suggested_fix_description="Review static analysis finding",
                    chunk_id="",
                    severity_score=SEVERITY_WEIGHTS.get(_map_static_severity(f["severity"]), 0.4),
                    detection_method=f.get("tool", "static"),
                ))
        return bugs + new_from_static

    def _calculate_risk_score(self, bugs: list[BugReport]) -> float:
        if not bugs:
            return 0.0
        raw = sum(SEVERITY_WEIGHTS.get(b.severity, 0.4) for b in bugs)
        # Normalize: score of 1.0 = 10+ critical bugs
        return min(round(raw / max(len(bugs), 1) * min(len(bugs) / 10, 1.0), 3), 1.0)


def _map_static_severity(s: str) -> BugSeverity:
    mapping = {
        "ERROR": BugSeverity.HIGH,
        "WARNING": BugSeverity.MEDIUM,
        "INFO": BugSeverity.LOW,
        "HIGH": BugSeverity.HIGH,
        "MEDIUM": BugSeverity.MEDIUM,
        "LOW": BugSeverity.LOW,
        "CRITICAL": BugSeverity.CRITICAL,
    }
    return mapping.get(s.upper(), BugSeverity.MEDIUM)
