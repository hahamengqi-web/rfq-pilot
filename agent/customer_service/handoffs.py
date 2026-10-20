"""Durable customer-support handoff queue backed by Redis."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class HandoffError(RuntimeError):
    pass


class CustomerHandoffService:
    """Store, claim, reply to and complete customer support handoffs."""

    def __init__(self, redis_url: str, prefix: str,
                 *, ttl_seconds: int = 30 * 24 * 60 * 60,
                 first_response_sla_seconds: int = 30 * 60) -> None:
        self.redis_url = redis_url
        self.prefix = prefix.rstrip(":")
        self.ttl_seconds = max(3600, int(ttl_seconds))
        self.first_response_sla_seconds = max(60, int(first_response_sla_seconds))
        self._client = None
        self._lock = asyncio.Lock()
        self._fallback: dict[str, dict[str, Any]] = {}

    @property
    def _records_key(self) -> str:
        return f"{self.prefix}:handoff-records"

    @property
    def _order_key(self) -> str:
        return f"{self.prefix}:handoff-order"

    async def _redis(self):
        if self._client is not None:
            return self._client
        if not self.redis_url:
            return None
        try:
            import redis.asyncio as redis
            client = redis.from_url(
                self.redis_url, decode_responses=True,
                socket_connect_timeout=1, socket_timeout=2,
            )
            await client.ping()
            self._client = client
            return client
        except Exception:
            self._client = None
            return None

    def _public(self, record: dict[str, Any]) -> dict[str, Any]:
        value = dict(record)
        value.pop("session_key", None)
        deadline = str(value.get("first_response_due_at") or "")
        has_reply = any(item.get("type") == "reply" for item in value.get("updates") or [])
        overdue = False
        if value.get("status") != "completed" and not has_reply and deadline:
            try:
                overdue = datetime.fromisoformat(deadline.replace("Z", "+00:00")) < datetime.now(timezone.utc)
            except ValueError:
                overdue = False
        value["first_response_sla"] = "overdue" if overdue else ("met" if has_reply else "pending")
        return value

    async def create(self, record: dict[str, Any]) -> dict[str, Any]:
        now = _utcnow()
        created_at = str(record.get("created_at") or now)
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            created = datetime.now(timezone.utc)
        value = {
            **dict(record),
            "handoff_id": str(record.get("handoff_id") or uuid.uuid4()),
            "status": "pending",
            "assigned_to": "",
            "created_at": created_at,
            "first_response_due_at": str(record.get("first_response_due_at") or (
                created + timedelta(seconds=self.first_response_sla_seconds)
            ).isoformat().replace("+00:00", "Z")),
            "updated_at": now,
            "version": 1,
            "updates": [],
        }
        customer_id = str(value.get("customer_id") or "").strip()
        conversation_id = str(value.get("conversation_id") or "").strip()
        if not customer_id or not conversation_id:
            raise HandoffError("handoff_customer_scope_missing")
        client = await self._redis()
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        score = time.time()
        if client is not None:
            await client.hset(self._records_key, value["handoff_id"], payload)
            await client.zadd(self._order_key, {value["handoff_id"]: score})
            await client.expire(self._records_key, self.ttl_seconds)
            await client.expire(self._order_key, self.ttl_seconds)
        else:
            async with self._lock:
                self._fallback[value["handoff_id"]] = value
        return self._public(value)

    async def _get_raw(self, handoff_id: str) -> dict[str, Any]:
        client = await self._redis()
        if client is not None:
            raw = await client.hget(self._records_key, handoff_id)
            if raw:
                return json.loads(raw)
        value = self._fallback.get(handoff_id)
        if value is None:
            raise HandoffError("handoff_not_found")
        return dict(value)

    async def get(self, handoff_id: str) -> dict[str, Any]:
        return self._public(await self._get_raw(handoff_id))

    async def list(self, *, status: str = "", limit: int = 100) -> list[dict[str, Any]]:
        allowed = {"", "pending", "assigned", "replied", "completed"}
        if status not in allowed:
            raise HandoffError("handoff_status_invalid")
        client = await self._redis()
        records: list[dict[str, Any]] = []
        if client is not None:
            ids = await client.zrevrange(self._order_key, 0, max(0, min(limit, 200) - 1))
            if ids:
                values = await client.hmget(self._records_key, ids)
                records = [json.loads(raw) for raw in values if raw]
        else:
            records = sorted(
                self._fallback.values(), key=lambda row: row.get("created_at", ""),
                reverse=True,
            )[:max(1, min(limit, 200))]
        if status:
            records = [row for row in records if row.get("status") == status]
        return [self._public(row) for row in records]

    async def _mutate(self, handoff_id: str,
                      transform: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        client = await self._redis()
        if client is None:
            async with self._lock:
                value = await self._get_raw(handoff_id)
                transform(value)
                value["version"] = int(value.get("version") or 0) + 1
                value["updated_at"] = _utcnow()
                self._fallback[handoff_id] = value
                return self._public(value)
        try:
            from redis.exceptions import WatchError
            for _ in range(4):
                async with client.pipeline(transaction=True) as pipe:
                    try:
                        await pipe.watch(self._records_key)
                        raw = await pipe.hget(self._records_key, handoff_id)
                        if not raw:
                            raise HandoffError("handoff_not_found")
                        value = json.loads(raw)
                        transform(value)
                        value["version"] = int(value.get("version") or 0) + 1
                        value["updated_at"] = _utcnow()
                        pipe.multi()
                        pipe.hset(
                            self._records_key, handoff_id,
                            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                        )
                        pipe.expire(self._records_key, self.ttl_seconds)
                        await pipe.execute()
                        return self._public(value)
                    except WatchError:
                        continue
            raise HandoffError("handoff_concurrent_update")
        except HandoffError:
            raise

    async def claim(self, handoff_id: str, operator: str) -> dict[str, Any]:
        operator = operator.strip()[:120]
        if not operator:
            raise HandoffError("handoff_operator_required")

        def apply(value: dict[str, Any]) -> None:
            if value.get("status") == "completed":
                raise HandoffError("handoff_already_completed")
            assigned = str(value.get("assigned_to") or "")
            if assigned and assigned != operator:
                raise HandoffError("handoff_already_claimed")
            value["assigned_to"] = operator
            if value.get("status") == "pending":
                value["status"] = "assigned"

        return await self._mutate(handoff_id, apply)

    async def reply(self, handoff_id: str, operator: str, message: str,
                    *, complete: bool = False) -> dict[str, Any]:
        operator, message = operator.strip()[:120], message.strip()[:4000]
        if not operator or not message:
            raise HandoffError("handoff_reply_required")

        def apply(value: dict[str, Any]) -> None:
            if value.get("status") == "completed":
                raise HandoffError("handoff_already_completed")
            assigned = str(value.get("assigned_to") or "")
            if assigned and assigned != operator:
                raise HandoffError("handoff_already_claimed")
            value["assigned_to"] = operator
            updates = list(value.get("updates") or [])
            updates.append({
                "update_id": str(uuid.uuid4()), "type": "reply",
                "message": message, "operator": operator, "created_at": _utcnow(),
            })
            value["updates"] = updates[-100:]
            value["status"] = "completed" if complete else "replied"

        return await self._mutate(handoff_id, apply)

    async def complete(self, handoff_id: str, operator: str) -> dict[str, Any]:
        operator = operator.strip()[:120]

        def apply(value: dict[str, Any]) -> None:
            assigned = str(value.get("assigned_to") or "")
            if assigned and assigned != operator:
                raise HandoffError("handoff_already_claimed")
            if value.get("status") == "completed":
                return
            value["assigned_to"] = operator or assigned
            value["status"] = "completed"
            updates = list(value.get("updates") or [])
            updates.append({
                "update_id": str(uuid.uuid4()), "type": "completed",
                "message": "", "operator": operator, "created_at": _utcnow(),
            })
            value["updates"] = updates[-100:]

        return await self._mutate(handoff_id, apply)

    async def customer_updates(self, customer_id: str, conversation_id: str,
                               *, limit: int = 100) -> list[dict[str, Any]]:
        records = await self.list(limit=200)
        updates: list[dict[str, Any]] = []
        for record in records:
            if (str(record.get("customer_id")) != customer_id
                    or str(record.get("conversation_id")) != conversation_id):
                continue
            for item in record.get("updates") or []:
                if item.get("type") not in {"reply", "completed"}:
                    continue
                updates.append({
                    "handoff_id": record["handoff_id"],
                    "status": record.get("status"),
                    "update_id": str(item.get("update_id") or ""),
                    "type": item.get("type"),
                    "message": str(item.get("message") or "")[:4000],
                    "created_at": item.get("created_at"),
                })
        updates.sort(key=lambda row: row.get("created_at") or "")
        return updates[-max(1, min(limit, 200)):]
