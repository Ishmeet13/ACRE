"""
CodeVectorStore
===============
Wraps ChromaDB for code chunk storage and semantic retrieval.
Uses HuggingFace code-specific embeddings (microsoft/codebert-base)
for better semantic understanding of code than generic text embeddings.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

import chromadb
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

# CodeBERT gives richer code semantics than text-ada-002 for this use case
CODE_EMBED_MODEL = "microsoft/codebert-base"
COLLECTION_PREFIX = "acre_repo_"


class CodeVectorStore:
    def __init__(self, host: str = "localhost", port: int = 8000):
        self.client = chromadb.HttpClient(
            host=host,
            port=port,
        )
        self._embedder: SentenceTransformer | None = None

    def _get_embedder(self) -> SentenceTransformer:
        if self._embedder is None:
            logger.info(f"Loading embedding model: {CODE_EMBED_MODEL}")
            self._embedder = SentenceTransformer(CODE_EMBED_MODEL)
        return self._embedder

    def _collection_name(self, analysis_id: str) -> str:
        # ChromaDB collection names must be ≤ 63 chars, alphanumeric + dash
        safe = hashlib.md5(analysis_id.encode()).hexdigest()[:16]
        return f"{COLLECTION_PREFIX}{safe}"

    # ── Write ─────────────────────────────────────────────────────────────────
    async def upsert_chunks(
        self,
        analysis_id: str,
        repo_url: str,
        chunks: list[dict],
        batch_size: int = 64,
    ) -> int:
        if not chunks:
            return 0

        col_name = self._collection_name(analysis_id)
        collection = self.client.get_or_create_collection(
            name=col_name,
            metadata={"repo_url": repo_url, "analysis_id": analysis_id},
        )

        embedder = self._get_embedder()
        total_indexed = 0

        for i in range(0, len(chunks), batch_size):
            batch = chunks[i : i + batch_size]
            # Build text for embedding — enriched document_text from CodeChunk
            texts = [_chunk_to_embed_text(c) for c in batch]
            ids = [c["chunk_id"] for c in batch]
            metadatas = [_chunk_to_metadata(c) for c in batch]

            # Embed synchronously (run in thread to avoid blocking event loop)
            embeddings = await asyncio.get_event_loop().run_in_executor(
                None, lambda t=texts: embedder.encode(t, show_progress_bar=False).tolist()
            )

            collection.upsert(
                ids=ids,
                embeddings=embeddings,
                documents=texts,
                metadatas=metadatas,
            )
            total_indexed += len(batch)
            logger.info(f"Indexed batch {i//batch_size + 1}: {total_indexed}/{len(chunks)} chunks")

        return total_indexed

    # ── Query ─────────────────────────────────────────────────────────────────
    def query(
        self,
        analysis_id: str,
        query_text: str,
        n_results: int = 10,
        where: dict | None = None,
    ) -> list[dict]:
        """
        Semantic search over a repository's indexed chunks.

        Returns list of dicts with keys:
          chunk_id, score, file_path, name, chunk_type, code, language, ...
        """
        col_name = self._collection_name(analysis_id)
        try:
            collection = self.client.get_collection(col_name)
        except Exception:
            return []

        embedder = self._get_embedder()
        query_embedding = embedder.encode([query_text]).tolist()

        results = collection.query(
            query_embeddings=query_embedding,
            n_results=n_results,
            where=where,
            include=["documents", "metadatas", "distances"],
        )

        output = []
        if not results["ids"] or not results["ids"][0]:
            return output

        for chunk_id, doc, meta, dist in zip(
            results["ids"][0],
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            output.append({
                "chunk_id": chunk_id,
                "score": round(1 - dist, 4),  # convert L2 distance to similarity
                "document": doc,
                **meta,
            })
        return output

    def query_by_file(self, analysis_id: str, file_path: str) -> list[dict]:
        """Get all chunks for a specific file — used when generating patches."""
        return self.query(
            analysis_id=analysis_id,
            query_text=file_path,
            n_results=50,
            where={"file_path": {"$eq": file_path}},
        )

    def list_files(self, analysis_id: str) -> list[str]:
        """Return all unique file paths indexed for an analysis."""
        col_name = self._collection_name(analysis_id)
        try:
            col = self.client.get_collection(col_name)
            all_meta = col.get(include=["metadatas"])
            files = list({m["file_path"] for m in all_meta["metadatas"]})
            return sorted(files)
        except Exception:
            return []

    def delete_analysis(self, analysis_id: str):
        col_name = self._collection_name(analysis_id)
        try:
            self.client.delete_collection(col_name)
        except Exception:
            pass


# ── Helpers ───────────────────────────────────────────────────────────────────
def _chunk_to_embed_text(chunk: dict) -> str:
    parts = [
        f"File: {chunk.get('file_path', '')}",
        f"Symbol: {chunk.get('chunk_type', '')} {chunk.get('name', '')}",
    ]
    if chunk.get("docstring"):
        parts.append(f"Docstring: {chunk['docstring']}")
    if chunk.get("imports"):
        parts.append("Imports:\n" + "\n".join(chunk["imports"][:5]))
    parts.append(chunk.get("code", ""))
    return "\n".join(parts)[:2000]  # ChromaDB has a doc length limit


def _chunk_to_metadata(chunk: dict) -> dict:
    """Flatten to scalar types — ChromaDB metadata values must be str/int/float/bool."""
    return {
        "file_path": str(chunk.get("file_path", "")),
        "language": str(chunk.get("language", "")),
        "chunk_type": str(chunk.get("chunk_type", "")),
        "name": str(chunk.get("name", "")),
        "start_line": int(chunk.get("start_line", 0)),
        "end_line": int(chunk.get("end_line", 0)),
        "complexity_score": int(chunk.get("complexity_score", 0)),
        "has_docstring": bool(chunk.get("docstring", "")),
    }
