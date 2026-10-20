"""Elasticsearch parent chunks, stored separately from searchable child chunks."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import httpx

from .config import RagKeywordConfig
from .contracts import Actor, CanonicalDocument, ParentChunk


PARENT_INDEX_DEFINITION = {
    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "parent_id": {"type": "keyword"},
            "document_id": {"type": "keyword"},
            "document_version": {"type": "integer"},
            "parent_text": {"type": "text", "index": False},
            "parent_location": {"type": "keyword", "index": False},
            "parent_content_hash": {"type": "keyword", "index": False},
            "parent_metadata": {"type": "object", "enabled": False},
            "business_unit_id": {"type": "keyword"},
            "allowed_roles": {"type": "keyword"},
            "classification": {"type": "keyword"},
            "document_status": {"type": "keyword"},
            "expires_at": {"type": "date"},
        },
    },
}


class ElasticsearchParentStore:
    def __init__(self, config: RagKeywordConfig, index: str, *, client: httpx.Client | None = None):
        config.validate()
        if config.backend != "elasticsearch":
            raise ValueError("ElasticsearchParentStore requires RAG_KEYWORD_BACKEND=elasticsearch")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", index):
            raise ValueError("RAG_ELASTICSEARCH_PARENT_INDEX is invalid")
        if index == config.index:
            raise ValueError("parent and child Elasticsearch indexes must differ")
        self.config = config
        self.index = index
        self._owns_client = client is None
        self.client = client or httpx.Client(
            base_url=config.url,
            auth=(config.username, config.password),
            timeout=config.timeout_seconds,
            trust_env=False,
            headers={"Accept": "application/json"},
        )
        self._index_ready = False

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _ensure_index(self) -> None:
        if self._index_ready:
            return
        response = self.client.head(f"/{self.index}")
        if response.status_code == 404:
            created = self.client.put(f"/{self.index}", json=PARENT_INDEX_DEFINITION)
            if created.status_code not in {200, 201}:
                if created.status_code != 400 or "resource_already_exists_exception" not in created.text:
                    created.raise_for_status()
        else:
            response.raise_for_status()
        self._index_ready = True

    @staticmethod
    def _document(parent: ParentChunk, source: CanonicalDocument) -> dict[str, object]:
        row: dict[str, object] = {
            "parent_id": parent.parent_id,
            "document_id": source.document_id,
            "document_version": source.version,
            "parent_text": parent.text,
            "parent_location": parent.location,
            "parent_content_hash": parent.content_hash,
            "parent_metadata": parent.metadata,
            "business_unit_id": source.business_unit_id,
            "allowed_roles": sorted(source.allowed_roles),
            "classification": source.classification,
            "document_status": source.status.value,
        }
        if source.expires_at is not None:
            row["expires_at"] = source.expires_at.isoformat()
        return row

    def upsert(self, parents, source: CanonicalDocument) -> int:
        rows = list(parents)
        if any(parent.document_id != source.document_id for parent in rows):
            raise ValueError("parent_document_mismatch")
        if not rows:
            return 0
        self._ensure_index()
        lines: list[str] = []
        for parent in rows:
            lines.append(json.dumps({"index": {"_index": self.index, "_id": parent.parent_id}}))
            lines.append(json.dumps(self._document(parent, source), ensure_ascii=False, default=str))
        response = self.client.post(
            "/_bulk",
            params={"refresh": "wait_for"},
            content="\n".join(lines) + "\n",
            headers={"Content-Type": "application/x-ndjson"},
        )
        response.raise_for_status()
        if response.json().get("errors"):
            raise RuntimeError("elasticsearch_parent_bulk_index_failed")
        return len(rows)

    def get(self, parent_id: str, actor: Actor | None = None) -> ParentChunk | None:
        if not re.fullmatch(r"[a-f0-9]{20}", parent_id):
            raise ValueError("invalid_parent_id")
        self._ensure_index()
        response = self.client.get(f"/{self.index}/_doc/{parent_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
        if not payload.get("found", True):
            return None
        row = payload["_source"]
        if actor is not None:
            if row["business_unit_id"] != actor.business_unit_id:
                return None
            if row["document_status"] not in {"approved", "published"}:
                return None
            if "customer" in actor.roles and row["classification"] != "public":
                return None
            roles = frozenset(row.get("allowed_roles") or ())
            if roles and not roles.intersection(actor.roles):
                return None
            expiry = row.get("expires_at")
            if expiry:
                expires_at = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at <= datetime.now(timezone.utc):
                    return None
        return ParentChunk(
            row["parent_id"], row["document_id"], row["parent_text"],
            row["parent_location"], row["parent_content_hash"],
            row.get("parent_metadata") or {},
        )

    def get_many(self, parent_ids, actor: Actor | None = None) -> list[ParentChunk]:
        return [parent for parent_id in parent_ids
                if (parent := self.get(parent_id, actor)) is not None]

    def delete_by_document(self, document_id: str, version: int | None = None) -> int:
        if version is not None and (isinstance(version, bool) or not isinstance(version, int) or version <= 0):
            raise ValueError("document version must be a positive integer")
        self._ensure_index()
        filters: list[dict[str, object]] = [{"term": {"document_id": document_id}}]
        if version is not None:
            filters.append({"term": {"document_version": version}})
        response = self.client.post(
            f"/{self.index}/_delete_by_query",
            params={"refresh": "true", "conflicts": "proceed"},
            json={"query": {"bool": {"filter": filters}}},
        )
        response.raise_for_status()
        return int(response.json().get("deleted", 0))
