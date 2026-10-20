"""Persistent, access-controlled parent chunks without a SQLite dependency."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .contracts import Actor, CanonicalDocument, ParentChunk


class FileParentStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, parent_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{20}", parent_id):
            raise ValueError("invalid_parent_id")
        return self.root / parent_id[:2] / f"{parent_id}.json"

    def upsert(self, parents, source: CanonicalDocument) -> int:
        rows = list(parents)
        if any(parent.document_id != source.document_id for parent in rows):
            raise ValueError("parent_document_mismatch")
        for parent in rows:
            path = self._path(parent.parent_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "parent": {
                    "parent_id": parent.parent_id, "document_id": parent.document_id,
                    "text": parent.text, "location": parent.location,
                    "content_hash": parent.content_hash, "metadata": parent.metadata,
                },
                "source": {
                    "document_id": source.document_id, "version": source.version,
                    "business_unit_id": source.business_unit_id,
                    "classification": source.classification,
                    "allowed_roles": sorted(source.allowed_roles),
                    "status": source.status.value,
                    "expires_at": source.expires_at.isoformat() if source.expires_at else None,
                },
            }
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return len(rows)

    def get(self, parent_id: str, actor: Actor | None = None) -> ParentChunk | None:
        path = self._path(parent_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        source = data["source"]
        if actor is not None:
            if (source["business_unit_id"] != actor.business_unit_id
                    or source["status"] not in {"approved", "published"}):
                return None
            if "customer" in actor.roles and source["classification"] != "public":
                return None
            roles = frozenset(source.get("allowed_roles") or ())
            if roles and not roles.intersection(actor.roles):
                return None
            expiry = source.get("expires_at")
            if expiry:
                expires_at = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at <= datetime.now(timezone.utc):
                    return None
        row = data["parent"]
        return ParentChunk(row["parent_id"], row["document_id"], row["text"],
                           row["location"], row["content_hash"], row.get("metadata") or {})

    def get_many(self, parent_ids, actor: Actor | None = None) -> list[ParentChunk]:
        return [parent for parent_id in parent_ids
                if (parent := self.get(parent_id, actor)) is not None]

    def delete_by_document(self, document_id: str, version: int | None = None) -> int:
        deleted = 0
        for path in self.root.glob("*/*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            source = data["source"]
            if source["document_id"] != document_id:
                continue
            if version is not None and int(source["version"]) != version:
                continue
            path.unlink(missing_ok=True)
            deleted += 1
        return deleted
