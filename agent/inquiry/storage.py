"""External storage boundaries: exact RFC822 bytes in S3, transient research in Redis."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import InquirySettings
from .repository import InquiryError, digest, encode, new_id, research_run_id


@dataclass(frozen=True)
class EmailArchive:
    bucket: str
    key: str
    sha256: str
    size: int
    version_id: str | None = None


class S3EmailArchive:
    def __init__(self, settings=None, client=None):
        self.settings = settings or InquirySettings.from_env()
        self.client = client

    def put(self, tenant, account, raw: bytes) -> dict:
        if not self.settings.s3_bucket:
            raise InquiryError("email_s3_bucket_not_configured", 503)
        if not raw or len(raw) > 30 * 1024 * 1024:
            raise InquiryError("email_original_missing_or_too_large", 422)
        if self.client is None:
            import boto3
            from botocore.config import Config
            self.client = boto3.client("s3", endpoint_url=self.settings.s3_endpoint,
                                      region_name=self.settings.s3_region,
                                      config=Config(connect_timeout=5, read_timeout=30,
                                                    retries={"max_attempts": 2},
                                                    s3={"addressing_style": "path"}))
        sha = hashlib.sha256(raw).hexdigest()
        scope = digest([tenant, account])
        key = f"{self.settings.s3_prefix}/{scope}/{sha}.eml"
        encryption = ({"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": self.settings.s3_kms_key}
                      if self.settings.s3_kms_key else {"ServerSideEncryption": "AES256"})
        # Content-addressed retries upload identical bytes. No public ACL or URL.
        try:
            result = self.client.put_object(Bucket=self.settings.s3_bucket, Key=key, Body=raw,
                                           ContentType="message/rfc822", Metadata={"sha256": sha},
                                           **encryption)
        except Exception as exc:
            raise InquiryError("email_s3_archive_failed", 503) from exc
        return asdict(EmailArchive(self.settings.s3_bucket, key, sha, len(raw), result.get("VersionId")))


class LocalDemoEmailArchive:
    """Content-addressed RFC822 archive for an explicitly enabled demo run.

    It deliberately has a different provider name from S3 so a demo message
    cannot be mistaken for a production archive in the workspace UI or audit
    artifacts.
    """

    def __init__(self, root="runtime/demo-mailbox"):
        self.root = Path(root).resolve()

    def put(self, tenant, account, raw: bytes) -> dict:
        if not raw or len(raw) > 30 * 1024 * 1024:
            raise InquiryError("email_original_missing_or_too_large", 422)
        sha = hashlib.sha256(raw).hexdigest()
        scope = digest([tenant, account])[:24]
        relative = Path(scope) / f"{sha}.eml"
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(raw)
        return {
            "provider": "local_demo_mailbox",
            "key": relative.as_posix(),
            "path": str(target),
            "sha256": sha,
            "size": len(raw),
            "demo_data": True,
        }


class RedisResearchMemory:
    def __init__(self, settings=None, client=None):
        self.settings = settings or InquirySettings.from_env()
        if client is None:
            import redis.asyncio as redis
            client = redis.from_url(self.settings.redis_url, decode_responses=True,
                                    socket_connect_timeout=5, socket_timeout=5)
        self.client = client

    def key(self, tenant, inquiry_id, revision):
        return "due_diligence:working:" + digest([tenant, inquiry_id, revision])

    async def load(self, tenant, inquiry_id, revision):
        raw = await self.client.get(self.key(tenant, inquiry_id, revision))
        if not raw:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise InquiryError("research_memory_invalid", 503)
        return value

    async def save(self, tenant, inquiry_id, revision, value):
        # Redis failure is visible. Never silently copy short-term chats into DB.
        await self.client.set(self.key(tenant, inquiry_id, revision), json.dumps(value, ensure_ascii=False),
                              ex=self.settings.memory_ttl)

    async def close(self):
        await self.client.aclose()


class DatabaseResearchMemory:
    """Authoritative research checkpoint plus normalized audit records."""

    def __init__(self, repository):
        self.repository = repository

    async def load(self, tenant, inquiry_id, revision):
        return await asyncio.to_thread(self._load, tenant, inquiry_id, revision)

    def _load(self, tenant, inquiry_id, revision):
        with self.repository.tx() as tx:
            row = tx.one(
                "SELECT checkpoint_json FROM inquiry_research_run WHERE tenant_id=? AND inquiry_id=? AND research_revision=?",
                (tenant, inquiry_id, int(revision)),
            )
        if not row:
            return None
        value = json.loads(row["checkpoint_json"] or "{}")
        return value if isinstance(value, dict) and value else None

    async def save(self, tenant, inquiry_id, revision, value):
        await asyncio.to_thread(self._save, tenant, inquiry_id, revision, value)

    def _save(self, tenant, inquiry_id, revision, value):
        if not isinstance(value, dict):
            raise InquiryError("research_checkpoint_invalid", 503)
        run_id = research_run_id(tenant, inquiry_id, revision)
        phase = str(value.get("phase") or "running")[:40]
        status = "complete" if phase == "complete" else "failed" if phase == "failed" else "running"
        report = value.get("report") if isinstance(value.get("report"), dict) else {}
        generation = report.get("report_generation", {}) if isinstance(report, dict) else {}
        now = time.time()
        with self.repository.tx() as tx:
            existing = tx.one("SELECT run_id,checkpoint_json FROM inquiry_research_run WHERE run_id=?", (run_id,))
            if existing:
                try:
                    previous_checkpoint = json.loads(existing.get("checkpoint_json") or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    previous_checkpoint = {}
                if previous_checkpoint.get("schema_version") != value.get("schema_version"):
                    tx.execute("DELETE FROM inquiry_research_step WHERE run_id=?", (run_id,)).close()
                    tx.execute("DELETE FROM inquiry_research_evidence WHERE run_id=?", (run_id,)).close()
            if not existing:
                tx.execute(
                    "INSERT INTO inquiry_research_run(run_id,tenant_id,inquiry_id,customer_id,research_revision,status,phase,checkpoint_json,started_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (run_id, tenant, inquiry_id, str(value.get("customer_id", ""))[:64], int(revision),
                     status, phase, encode(value), now, now),
                ).close()
            tx.execute(
                "UPDATE inquiry_research_run SET status=?,phase=?,checkpoint_json=?,report_model=?,fallback_used=?,error_code=NULL,updated_at=?,finished_at=? WHERE run_id=?",
                (status, phase, encode(value), str(report.get("report_model", ""))[:160] or None,
                 1 if generation.get("fallback_used") else 0, now,
                 now if status == "complete" else None, run_id),
            ).close()

            plan = value.get("plan", {}) if isinstance(value.get("plan"), dict) else {}
            executions = {row.get("step_id"): row for row in value.get("executions", [])
                          if isinstance(row, dict) and row.get("step_id")}
            active = value.get("active_step") if isinstance(value.get("active_step"), dict) else None
            for step in (plan.get("steps", []) if isinstance(plan.get("steps", []), list) else []):
                if not isinstance(step, dict) or not step.get("id"):
                    continue
                step_id = str(step["id"])[:80]
                execution = executions.get(step_id)
                is_active = bool(active and active.get("id") == step_id)
                step_status = str(execution.get("status"))[:32] if execution else "running" if is_active else "pending"
                reason = str((execution or {}).get("reason") or step.get("reason", ""))[:500]
                query = str(step.get("query", ""))[:500]
                stored = tx.one("SELECT * FROM inquiry_research_step WHERE run_id=? AND step_id=?",
                                (run_id, step_id))
                evidence_count = len((execution or {}).get("evidence_ids", []))
                if stored:
                    attempts = int(stored.get("attempt_count") or 0) + (1 if is_active else 0)
                    tx.execute(
                        "UPDATE inquiry_research_step SET tool=?,query_hash=?,query_summary=?,status=?,reason=?,evidence_count=?,attempt_count=?,started_at=?,finished_at=?,updated_at=? WHERE run_id=? AND step_id=?",
                        (str(step.get("tool", ""))[:40], digest(query), query, step_status, reason,
                         evidence_count, attempts,
                         stored.get("started_at") or ((active or {}).get("started_at") if is_active else None),
                         now if execution else None, now, run_id, step_id),
                    ).close()
                else:
                    tx.execute(
                        "INSERT INTO inquiry_research_step(step_record_id,run_id,step_id,tool,query_hash,query_summary,status,reason,evidence_count,attempt_count,started_at,finished_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (new_id("rstep"), run_id, step_id, str(step.get("tool", ""))[:40], digest(query),
                         query, step_status, reason, evidence_count, 1 if is_active else 0,
                         (active or {}).get("started_at") if is_active else None,
                         now if execution else None, now),
                    ).close()

            for evidence in (value.get("evidence", []) if isinstance(value.get("evidence", []), list) else []):
                if not isinstance(evidence, dict) or not evidence.get("evidence_id"):
                    continue
                evidence_id = str(evidence["evidence_id"])[:96]
                payload = encode(evidence)
                stored = tx.one(
                    "SELECT evidence_row_id FROM inquiry_research_evidence WHERE run_id=? AND evidence_id=?",
                    (run_id, evidence_id),
                )
                values = (
                    str(evidence.get("tool_step_id", ""))[:80] or None,
                    str(evidence.get("company_identity_id", ""))[:64] or None,
                    str(evidence.get("source_type", "unverified"))[:40],
                    str(evidence.get("quality", "unverified"))[:40],
                    str(evidence.get("url", ""))[:4000] or None,
                    str(evidence.get("sha256") or digest(evidence))[:64],
                    payload,
                    float(evidence.get("collected_at") or evidence.get("created_at") or now),
                )
                if stored:
                    tx.execute(
                        "UPDATE inquiry_research_evidence SET step_id=?,company_identity_id=?,source_type=?,quality=?,url=?,content_hash=?,payload_json=?,collected_at=? WHERE run_id=? AND evidence_id=?",
                        (*values, run_id, evidence_id),
                    ).close()
                else:
                    tx.execute(
                        "INSERT INTO inquiry_research_evidence(evidence_row_id,run_id,evidence_id,step_id,company_identity_id,source_type,quality,url,content_hash,payload_json,collected_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (new_id("evidence"), run_id, evidence_id, *values),
                    ).close()


class DurableResearchMemory:
    """MySQL/SQLite authority with Redis as a disposable acceleration cache."""

    def __init__(self, database: DatabaseResearchMemory, cache: RedisResearchMemory | None = None):
        self.database = database
        self.cache = cache

    async def load(self, tenant, inquiry_id, revision):
        value = await self.database.load(tenant, inquiry_id, revision)
        if value is not None or self.cache is None:
            return value
        try:
            value = await self.cache.load(tenant, inquiry_id, revision)
        except Exception:
            logging.getLogger(__name__).warning("Research Redis cache unavailable during load")
            return None
        if value is not None:
            await self.database.save(tenant, inquiry_id, revision, value)
        return value

    async def save(self, tenant, inquiry_id, revision, value):
        await self.database.save(tenant, inquiry_id, revision, value)
        if self.cache is None:
            return
        try:
            await self.cache.save(tenant, inquiry_id, revision, value)
        except Exception:
            logging.getLogger(__name__).warning("Research Redis cache unavailable during save")

    async def close(self):
        if self.cache is not None:
            await self.cache.close()
