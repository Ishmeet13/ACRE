"""
Architecture Analyzer Agent
============================
Builds a structural map of the repository:
  - Module dependency graph
  - Architecture pattern detection (MVC, layered, microservices, monolith)
  - Entry point identification
  - High-complexity file hotspots
  - Language distribution
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from collections import defaultdict

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from vector_store_client import get_vector_store

logger = logging.getLogger(__name__)


@dataclass
class ArchitectureResult:
    architecture_map: dict
    high_complexity_files: list[str]


class ArchitectureAnalyzer:
    def __init__(self, analysis_id: str):
        self.analysis_id = analysis_id
        self.vector_store = get_vector_store()
        self.llm = ChatOpenAI(model="gpt-4o", temperature=0, api_key=os.getenv("OPENAI_API_KEY"))

    async def run(self) -> ArchitectureResult:
        files = self.vector_store.list_files(self.analysis_id)
        if not files:
            return ArchitectureResult(architecture_map={}, high_complexity_files=[])

        # Build language distribution
        lang_dist = defaultdict(int)
        for f in files:
            ext = f.rsplit(".", 1)[-1] if "." in f else "unknown"
            lang_dist[ext] += 1

        # Find high-complexity chunks via vector store metadata
        # Query for chunks with high complexity scores
        complex_chunks = self.vector_store.query(
            self.analysis_id,
            query_text="complex function with many branches loops conditionals error handling",
            n_results=30,
        )

        # Group by file and sum complexity
        file_complexity: dict[str, int] = defaultdict(int)
        for chunk in complex_chunks:
            file_complexity[chunk.get("file_path", "")] += chunk.get("complexity_score", 0)

        high_complexity_files = sorted(
            file_complexity.keys(),
            key=lambda f: file_complexity[f],
            reverse=True
        )[:20]

        # Detect architecture pattern via LLM on file list sample
        arch_map = await self._detect_architecture(files, lang_dist, high_complexity_files)

        return ArchitectureResult(
            architecture_map=arch_map,
            high_complexity_files=high_complexity_files,
        )

    async def _detect_architecture(
        self,
        files: list[str],
        lang_dist: dict,
        high_complexity_files: list[str],
    ) -> dict:
        file_sample = "\n".join(files[:80])
        system = """You are a software architect. Analyze a repository's file structure and return a JSON object.

Return ONLY valid JSON with this structure:
{
  "summary": "2-3 sentence description of the codebase",
  "architecture_pattern": "monolith|mvc|layered|microservices|event_driven|library|cli_tool",
  "primary_language": "python|javascript|typescript|java|go|rust|other",
  "entry_points": ["list", "of", "likely", "entry", "point", "files"],
  "key_modules": ["list", "of", "important", "module", "directories"],
  "concerns": ["list of architectural concerns or risks"],
  "complexity_hotspots": ["top files with most complexity"],
  "dependency_style": "requirements.txt|package.json|go.mod|pom.xml|Cargo.toml|mixed"
}"""

        msg = HumanMessage(content=f"""File structure (sample of {len(files)} files):
{file_sample}

Language distribution: {dict(lang_dist)}
High complexity files: {high_complexity_files[:10]}

Analyze this repository structure.""")

        try:
            resp = await self.llm.ainvoke([SystemMessage(content=system), msg])
            raw = resp.content.strip()
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0].strip()
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()
            arch_map = json.loads(raw)
            arch_map["file_count"] = len(files)
            arch_map["language_distribution"] = dict(lang_dist)
            return arch_map
        except Exception as e:
            logger.warning(f"Architecture analysis LLM failed: {e}")
            return {
                "summary": f"Repository with {len(files)} files",
                "architecture_pattern": "unknown",
                "primary_language": max(lang_dist, key=lang_dist.get) if lang_dist else "unknown",
                "file_count": len(files),
                "language_distribution": dict(lang_dist),
                "entry_points": [],
                "key_modules": [],
                "concerns": [],
                "complexity_hotspots": high_complexity_files[:5],
            }
