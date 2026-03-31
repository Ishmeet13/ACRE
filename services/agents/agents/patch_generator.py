"""
Patch Generator Agent
=====================
Generates code patches for detected bugs.

Primary path:  Fine-tuned CodeLlama-7B (via HuggingFace / local Ollama)
Fallback path: GPT-4o for complex multi-file, multi-hunk changes

Output: Unified diff patches (.patch format) + natural language explanation
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Optional

import mlflow
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from state import BugReport, Patch, PatchStatus
from vector_store_client import get_vector_store

logger = logging.getLogger(__name__)

FINETUNED_MODEL_PATH = os.getenv("FINETUNED_MODEL_PATH", "")
USE_FINETUNED = bool(FINETUNED_MODEL_PATH)


@dataclass
class PatchGenerationResult:
    patches: list[Patch]


class PatchGeneratorAgent:
    def __init__(
        self,
        analysis_id: str,
        bug_reports: list[BugReport],
        retry_patch_ids: list[str] | None = None,
    ):
        self.analysis_id = analysis_id
        self.bug_reports = bug_reports
        self.retry_patch_ids = set(retry_patch_ids or [])
        self.vector_store = get_vector_store()
        self.gpt4o = ChatOpenAI(model="gpt-4o", temperature=0.1, api_key=os.getenv("OPENAI_API_KEY"))
        self._finetuned_model = None

    async def run(self) -> PatchGenerationResult:
        """Generate patches for all bugs (or retry failed ones)."""
        targets = self.bug_reports
        if self.retry_patch_ids:
            # If retrying, only regenerate for bugs whose patches failed
            targets = [b for b in self.bug_reports if b.bug_id in self.retry_patch_ids]

        patches: list[Patch] = []

        for bug in targets:
            patch = await self._generate_patch(bug)
            if patch:
                patches.append(patch)

        logger.info(f"[{self.analysis_id}] Generated {len(patches)} patches for {len(targets)} bugs")
        return PatchGenerationResult(patches=patches)

    async def _generate_patch(self, bug: BugReport) -> Optional[Patch]:
        """Generate a single patch for a bug, trying fine-tuned model first."""
        # Retrieve surrounding code context from ChromaDB
        context_chunks = self.vector_store.query(
            analysis_id=self.analysis_id,
            query_text=f"{bug.title} {bug.root_cause} {bug.file_path}",
            n_results=5,
        )
        file_chunks = self.vector_store.query_by_file(self.analysis_id, bug.file_path)

        context = _build_context(bug, context_chunks, file_chunks)

        with mlflow.start_run(nested=True, run_name=f"patch_{bug.bug_id[:8]}"):
            mlflow.log_params({
                "bug_id": bug.bug_id,
                "severity": bug.severity.value,
                "bug_type": bug.bug_type,
                "file_path": bug.file_path,
                "use_finetuned": USE_FINETUNED,
            })

            # Try fine-tuned model first
            patch_content = None
            model_used = "gpt-4o"

            if USE_FINETUNED:
                try:
                    patch_content = await self._finetuned_generate(context)
                    model_used = "codellama-finetuned"
                except Exception as e:
                    logger.warning(f"Fine-tuned model failed for {bug.bug_id}: {e}. Falling back to GPT-4o.")

            if patch_content is None:
                patch_content = await self._gpt4o_generate(context)

            if not patch_content:
                return None

            # Parse the response into structured Patch
            patch = _parse_patch_response(patch_content, bug, self.analysis_id, model_used)

            mlflow.log_text(context, "patch_context.txt")
            mlflow.log_text(patch_content, "patch_raw_response.txt")
            if patch:
                mlflow.log_text(patch.unified_diff, "patch.diff")
                mlflow.log_metric("confidence_score", patch.confidence_score)

        return patch

    async def _finetuned_generate(self, context: str) -> str:
        """
        Call local fine-tuned CodeLlama model.
        In production this hits an Ollama endpoint or HuggingFace inference server.
        """
        import httpx
        ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
        model_name = os.getenv("FINETUNED_MODEL_NAME", "codellama-acre")

        payload = {
            "model": model_name,
            "prompt": context,
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 1024},
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{ollama_url}/api/generate", json=payload)
            resp.raise_for_status()
            return resp.json()["response"]

    async def _gpt4o_generate(self, context: str) -> Optional[str]:
        system = """You are an expert software engineer specializing in bug fixes and security patches.

Given a bug report and surrounding code context, generate a complete, correct patch.

Your response MUST be valid JSON with this exact structure:
{
  "explanation": "clear explanation of what the bug is and how the fix works",
  "original_code": "the exact buggy code snippet",
  "fixed_code": "the corrected code snippet",
  "confidence_score": 0.0-1.0,
  "risk_level": "low|medium|high",
  "additional_notes": "any caveats or related changes needed",
  "test_hint": "what to test to verify this fix"
}

Rules:
- original_code and fixed_code must be valid, complete code (not pseudo-code)
- Be conservative: minimal changes, preserve existing style
- If the fix requires changes to multiple locations, include all in fixed_code with clear comments
- confidence_score reflects how certain you are the fix is correct"""

        try:
            messages = [
                SystemMessage(content=system),
                HumanMessage(content=context),
            ]
            response = await self.gpt4o.ainvoke(messages)
            return response.content.strip()
        except Exception as e:
            logger.error(f"GPT-4o patch generation failed: {e}")
            return None


# ── Helpers ───────────────────────────────────────────────────────────────────
def _build_context(bug: BugReport, context_chunks: list[dict], file_chunks: list[dict]) -> str:
    """Build rich prompt context for patch generation."""
    file_code = "\n\n".join(
        c.get("document", "") for c in file_chunks[:3]
    )
    related_code = "\n\n---\n\n".join(
        f"# Related: {c.get('file_path')} → {c.get('name')}\n{c.get('document', '')}"
        for c in context_chunks[:3]
        if c.get("file_path") != bug.file_path
    )
    return f"""## Bug Report

**ID**: {bug.bug_id}
**Title**: {bug.title}
**Severity**: {bug.severity.value}
**Type**: {bug.bug_type}
**File**: {bug.file_path} (lines {bug.start_line}–{bug.end_line})

**Description**: {bug.description}

**Root Cause**: {bug.root_cause}

**Vulnerable Code**:
```
{bug.vulnerable_code}
```

**Suggested Fix Direction**: {bug.suggested_fix_description}

## Full File Context

```
{file_code[:3000]}
```

## Related Code (other files)

{related_code[:2000]}

---

Generate a complete, minimal patch to fix this bug. Return valid JSON as specified."""


def _parse_patch_response(raw: str, bug: BugReport, analysis_id: str, model_used: str) -> Optional[Patch]:
    """Parse LLM JSON response into a structured Patch object."""
    try:
        # Strip markdown code blocks if present
        cleaned = raw
        if "```json" in cleaned:
            cleaned = cleaned.split("```json")[1].split("```")[0].strip()
        elif "```" in cleaned:
            cleaned = cleaned.split("```")[1].split("```")[0].strip()

        data = json.loads(cleaned)

        original = data.get("original_code", bug.vulnerable_code)
        fixed = data.get("fixed_code", "")
        if not fixed:
            return None

        # Generate unified diff
        diff = "\n".join(difflib.unified_diff(
            original.splitlines(keepends=True),
            fixed.splitlines(keepends=True),
            fromfile=f"a/{bug.file_path}",
            tofile=f"b/{bug.file_path}",
            lineterm="",
        ))

        return Patch(
            patch_id=str(uuid.uuid4()),
            bug_id=bug.bug_id,
            analysis_id=analysis_id,
            file_path=bug.file_path,
            original_code=original,
            fixed_code=fixed,
            unified_diff=diff,
            explanation=data.get("explanation", ""),
            confidence_score=float(data.get("confidence_score", 0.5)),
            risk_level=data.get("risk_level", "medium"),
            additional_notes=data.get("additional_notes", ""),
            test_hint=data.get("test_hint", ""),
            model_used=model_used,
            status=PatchStatus.PENDING,
        )
    except Exception as e:
        logger.warning(f"Failed to parse patch response: {e}\nRaw: {raw[:200]}")
        return None
