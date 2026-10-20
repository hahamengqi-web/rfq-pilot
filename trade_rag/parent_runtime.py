from __future__ import annotations

from .config import RagVectorConfig, load_rag_keyword_config, load_rag_vector_config
from .stores import InMemoryParentStore
from .file_parent_store import FileParentStore
import os
from pathlib import Path


def create_application_parent_store(config: RagVectorConfig | None = None):
    current = config or load_rag_vector_config()
    current.validate()
    if current.backend == "sqlite":
        from .sqlite_store import SqliteParentStore
        return SqliteParentStore(current)
    if current.backend == "memory":
        return InMemoryParentStore()
    keyword_config = load_rag_keyword_config()
    if keyword_config.backend == "elasticsearch":
        from .elasticsearch_parent_store import ElasticsearchParentStore
        index = os.environ.get("RAG_ELASTICSEARCH_PARENT_INDEX", "trade_knowledge_parent_v1").strip().lower()
        return ElasticsearchParentStore(keyword_config, index)
    from .mysql_metadata import load_rag_metadata_config
    metadata_config = load_rag_metadata_config()
    if metadata_config.backend == "mysql":
        from .mysql_parent_store import MySQLParentStore
        return MySQLParentStore(metadata_config)
    root = os.environ.get("RAG_PARENT_STORE_PATH") or str(
        Path(os.environ.get("NANOCLAW_KNOWLEDGE_FILE_ROOT", "workspace/knowledge_base")) / "parents"
    )
    path = Path(root)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    return FileParentStore(path)
