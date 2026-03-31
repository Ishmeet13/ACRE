"""
Shared State Models
===================
Pydantic + dataclass models shared across all agent nodes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class BugSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH     = "HIGH"
    MEDIUM   = "MEDIUM"
    LOW      = "LOW"
    INFO     = "INFO"


class PatchStatus(str, Enum):
    PENDING  = "PENDING"
    PASSED   = "PASSED"
    PARTIAL  = "PARTIAL"
    FAILED   = "FAILED"
    SKIPPED  = "SKIPPED"


@dataclass
class BugReport:
    bug_id: str
    analysis_id: str
    file_path: str
    start_line: int
    end_line: int
    title: str
    description: str
    severity: BugSeverity
    bug_type: str                     # security | logic | reliability | performance | code_smell
    vulnerable_code: str
    root_cause: str
    suggested_fix_description: str
    chunk_id: str
    severity_score: float             # 0-1 numeric weight
    detection_method: str = "llm"    # llm | static | pattern_rag


@dataclass
class Patch:
    patch_id: str
    bug_id: str
    analysis_id: str
    file_path: str
    original_code: str
    fixed_code: str
    unified_diff: str
    explanation: str
    confidence_score: float
    risk_level: str                   # low | medium | high
    additional_notes: str
    test_hint: str
    model_used: str                   # gpt-4o | codellama-finetuned
    status: PatchStatus = PatchStatus.PENDING


@dataclass
class AnalysisState:
    """Top-level state persisted to Postgres."""
    analysis_id: str
    repo_url: str
    branch: str
    status: str                       # running | done | error
    risk_score: float = 0.0
    bugs_found: int = 0
    patches_generated: int = 0
    patches_passing: int = 0
    created_at: str = ""
    completed_at: str = ""
    error_message: str = ""
