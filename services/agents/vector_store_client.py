"""Singleton accessor for the CodeVectorStore used by all agent nodes."""
import os
from functools import lru_cache

import chromadb
from sentence_transformers import SentenceTransformer


class CodeVectorStore:
    def __init__(self, host: str = "localhost", port: int = 8000):
        self.client = chromadb.HttpClient(host=host, port=port)
        self._embedder: SentenceTransformer | None = None

    def _get_embedder(self) -> SentenceTransformer:
        if self._embedder is None:
            self._embedder = SentenceTransformer("microsoft/codebert-base")
        return self._embedder

    def query(self, analysis_id: str, query_text: str, n_results: int = 10, where: dict | None = None) -> list[dict]:
        import hashlib
        safe = hashlib.md5(analysis_id.encode()).hexdigest()[:16]
        col_name = f"acre_repo_{safe}"
        try:
            collection = self.client.get_collection(col_name)
        except Exception:
            return []
        embedder = self._get_embedder()
        query_embedding = embedder.encode([query_text]).tolist()
        results = collection.query(
            query_embeddings=query_embedding,
            n_results=n_results,
            include=["documents", "metadatas", "distances"],
        )
        output = []
        if not results["ids"] or not results["ids"][0]:
            return output
        for chunk_id, doc, meta, dist in zip(
            results["ids"][0], results["documents"][0],
            results["metadatas"][0], results["distances"][0],
        ):
            output.append({"chunk_id": chunk_id, "score": round(1 - dist, 4), "document": doc, **meta})
        return output

    def query_by_file(self, analysis_id: str, file_path: str) -> list[dict]:
        return self.query(analysis_id, file_path, n_results=50,
                         where={"file_path": {"$eq": file_path}})

    def list_files(self, analysis_id: str) -> list[str]:
        import hashlib
        safe = hashlib.md5(analysis_id.encode()).hexdigest()[:16]
        col_name = f"acre_repo_{safe}"
        try:
            col = self.client.get_collection(col_name)
            all_meta = col.get(include=["metadatas"])
            return sorted({m["file_path"] for m in all_meta["metadatas"]})
        except Exception:
            return []

    def delete_analysis(self, analysis_id: str):
        import hashlib
        safe = hashlib.md5(analysis_id.encode()).hexdigest()[:16]
        try:
            self.client.delete_collection(f"acre_repo_{safe}")
        except Exception:
            pass


@lru_cache(maxsize=1)
def get_vector_store() -> CodeVectorStore:
    return CodeVectorStore(
        host=os.getenv("CHROMA_HOST", "localhost"),
        port=int(os.getenv("CHROMA_PORT", "8001")),
    )