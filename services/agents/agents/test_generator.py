"""
Test Generator Agent
====================
Generates pytest test cases that validate each patch.

For each patch:
  - A regression test that reproduces the bug with the original code
  - A fix verification test that passes with the patched code
  - Edge case tests derived from the bug type
  - Integration hints for multi-file changes
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from state import BugReport, Patch

logger = logging.getLogger(__name__)


@dataclass
class TestGenerationResult:
    test_cases: list[dict]


class TestGeneratorAgent:
    def __init__(
        self,
        analysis_id: str,
        patches: list[Patch],
        bug_reports: list[BugReport],
    ):
        self.analysis_id = analysis_id
        self.patches = patches
        self.bug_map = {b.bug_id: b for b in bug_reports}
        self.llm = ChatOpenAI(model="gpt-4o", temperature=0.2, api_key=os.getenv("OPENAI_API_KEY"))

    async def run(self) -> TestGenerationResult:
        test_cases = []
        for patch in self.patches:
            bug = self.bug_map.get(patch.bug_id)
            test_case = await self._generate_test(patch, bug)
            if test_case:
                test_cases.append(test_case)
        logger.info(f"[{self.analysis_id}] Generated {len(test_cases)} test cases")
        return TestGenerationResult(test_cases=test_cases)

    async def _generate_test(self, patch: Patch, bug: BugReport | None) -> dict | None:
        bug_context = ""
        if bug:
            bug_context = f"""Bug: {bug.title}
Type: {bug.bug_type}
Severity: {bug.severity.value}
Root cause: {bug.root_cause}
"""
        system = """You are a senior software engineer writing pytest tests.

Generate a pytest test file that:
1. Tests the FIXED code works correctly
2. Verifies the bug scenario is handled
3. Includes edge cases

Return ONLY valid Python test code (no markdown, no explanations).
The test file should be self-contained and not import from external project files.
Define any helper functions or mock data inline.
Use pytest conventions: functions named test_*, clear assert messages."""

        user_content = f"""{bug_context}
Original (buggy) code:
```python
{patch.original_code[:1500]}
```

Fixed code:
```python
{patch.fixed_code[:1500]}
```

Test hint: {patch.test_hint}

Generate a pytest test file for the fixed code."""

        try:
            resp = await self.llm.ainvoke([
                SystemMessage(content=system),
                HumanMessage(content=user_content),
            ])
            test_code = resp.content.strip()
            # Strip markdown if present
            if "```python" in test_code:
                test_code = test_code.split("```python")[1].split("```")[0].strip()
            elif "```" in test_code:
                test_code = test_code.split("```")[1].split("```")[0].strip()

            return {
                "patch_id": patch.patch_id,
                "bug_id":   patch.bug_id,
                "test_code": test_code,
                "file_path": f"test_patch_{patch.patch_id[:8]}.py",
            }
        except Exception as e:
            logger.warning(f"Test generation failed for patch {patch.patch_id}: {e}")
            return None
