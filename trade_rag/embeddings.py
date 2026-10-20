from __future__ import annotations
import hashlib, json, math, os, threading
from pathlib import Path
from typing import Protocol

from .config import RagApiConfig, load_rag_api_config

class EmbeddingProvider(Protocol):
    model_id: str
    def embed(self, texts: list[str]) -> list[list[float]]: ...

class MockEmbeddingProvider:
    model_id = "mock-hash-v1"
    def __init__(self, dimensions: int = 64): self.dimensions = dimensions
    def embed(self, texts, progress_callback=None):
        out=[]
        for index, text in enumerate(texts, start=1):
            vec=[0.0]*self.dimensions
            for token in text.lower().split(): vec[int(hashlib.sha256(token.encode()).hexdigest(),16)%self.dimensions]+=1
            norm=math.sqrt(sum(x*x for x in vec)) or 1; out.append([x/norm for x in vec])
            if progress_callback is not None:
                progress_callback(index, len(texts))
        return out


class FileEmbeddingCache:
    """Content-addressed vector cache; stores no source text or API credentials."""

    def __init__(self, root: Path, model_id: str, dimensions: int):
        namespace = hashlib.sha256(
            f"{model_id}|{dimensions}".encode("utf-8")
        ).hexdigest()[:20]
        self.root = root / namespace
        self.dimensions = dimensions
        self._lock = threading.RLock()

    def _path(self, text: str) -> Path:
        key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return self.root / key[:2] / f"{key}.json"

    def get(self, text: str) -> list[float] | None:
        path = self._path(text)
        if not path.is_file():
            return None
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(values, list) or len(values) != self.dimensions
                    or any(isinstance(value, bool) or not isinstance(value, (int, float))
                           or not math.isfinite(float(value)) for value in values)):
                return None
            return [float(value) for value in values]
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def put(self, text: str, vector: list[float]) -> None:
        if len(vector) != self.dimensions:
            return
        path = self._path(text)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(f".{os.getpid()}-{threading.get_ident()}.tmp")
            try:
                temporary.write_text(
                    json.dumps(vector, ensure_ascii=True, separators=(",", ":")),
                    encoding="utf-8",
                )
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)


class Qwen3EmbeddingProvider:
    """SiliconFlow-compatible Qwen3 text embeddings for indexing and retrieval."""

    QUERY_INSTRUCTION = (
        "Instruct: Retrieve product information relevant to answering customer "
        "product and after-sales questions.\nQuery: "
    )

    def __init__(self, config: RagApiConfig | None = None, *, client=None):
        self.config = config or load_rag_api_config()
        self.client = client
        self.model_id = self.config.embedding.model
        self.dimensions = self.config.embedding_dimensions
        cache_enabled = os.environ.get("RAG_EMBEDDING_CACHE_ENABLED", "true").strip().lower() in {
            "1", "true", "yes", "on",
        }
        cache_root = Path(os.environ.get(
            "RAG_EMBEDDING_CACHE_PATH", "workspace/knowledge_base/embedding_cache",
        ))
        if not cache_root.is_absolute():
            cache_root = Path(__file__).resolve().parent.parent / cache_root
        self.cache = (FileEmbeddingCache(cache_root, self.model_id, self.dimensions)
                      if cache_enabled else None)

    def embed(self, texts: list[str], progress_callback=None) -> list[list[float]]:
        if not texts:
            return []
        if not self.config.remote_data_transfer_approved:
            raise RuntimeError("rag_embedding_remote_transfer_not_approved")
        remote = self.config.embedding
        if not remote.configured:
            raise RuntimeError("rag_embedding_api_not_configured")
        if not all(isinstance(text, str) and text.strip() for text in texts):
            raise ValueError("rag_embedding_input_invalid")
        import httpx

        ordered: list[list[float] | None] = [None] * len(texts)
        missing: list[int] = []
        for index, text in enumerate(texts):
            cached = self.cache.get(text) if self.cache is not None else None
            if cached is None:
                missing.append(index)
            else:
                ordered[index] = cached
        completed = len(texts) - len(missing)
        if progress_callback is not None:
            progress_callback(completed, len(texts))
        for start in range(0, len(missing), self.config.embedding_batch_size):
            indexes = missing[start:start + self.config.embedding_batch_size]
            batch = [texts[index] for index in indexes]
            payload = {
                "model": remote.model,
                "input": batch,
                "dimensions": self.dimensions,
                "encoding_format": "float",
            }
            try:
                http = self.client or httpx
                response = http.post(
                    f"{remote.base_url}/embeddings",
                    headers={"Authorization": f"Bearer {remote.api_key}"},
                    json=payload,
                    timeout=remote.timeout_seconds,
                )
                response.raise_for_status()
                data = response.json()["data"]
                if not isinstance(data, list) or len(data) != len(batch):
                    raise ValueError("embedding_response_count_invalid")
                batch_vectors: list[list[float] | None] = [None] * len(batch)
                for row in data:
                    index = row["index"]
                    values = row["embedding"]
                    if (not isinstance(index, int) or isinstance(index, bool)
                            or not 0 <= index < len(batch) or batch_vectors[index] is not None
                            or not isinstance(values, list)
                            or len(values) != self.dimensions):
                        raise ValueError("embedding_response_shape_invalid")
                    vector = [float(value) for value in values]
                    if not all(math.isfinite(value) for value in vector):
                        raise ValueError("embedding_response_value_invalid")
                    batch_vectors[index] = vector
                if any(vector is None for vector in batch_vectors):
                    raise ValueError("embedding_response_index_invalid")
                for source_index, vector in zip(indexes, batch_vectors):
                    if vector is None:
                        raise ValueError("embedding_response_index_invalid")
                    ordered[source_index] = vector
                    if self.cache is not None:
                        self.cache.put(texts[source_index], vector)
                completed += len(indexes)
                if progress_callback is not None:
                    progress_callback(completed, len(texts))
            except Exception as exc:
                raise RuntimeError("rag_embedding_request_failed") from exc
        if any(vector is None for vector in ordered):
            raise RuntimeError("rag_embedding_cache_incomplete")
        return [vector for vector in ordered if vector is not None]

    def embed_query(self, query: str) -> list[float]:
        return self.embed([self.QUERY_INSTRUCTION + query])[0]


def create_application_embedding_provider():
    import os

    config = load_rag_api_config()
    provider = os.environ.get("RAG_EMBEDDING_PROVIDER", "mock").strip().lower()
    if provider == "qwen3":
        return Qwen3EmbeddingProvider(config)
    if provider == "mock":
        return MockEmbeddingProvider()
    raise ValueError("RAG_EMBEDDING_PROVIDER must be qwen3 or mock")
