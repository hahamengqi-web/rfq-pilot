"""Redis short-term memory and consent-gated MySQL long-term memory."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from agent.memory_runtime.models import MemoryScope
from agent.memory_runtime.services.customer_memory import CustomerMemoryService

from .models import ConversationContext
from .settings import CustomerHarnessSettings
from .handoffs import CustomerHandoffService


class RedisConversationMemory:
    """Keep 15 summarized turns, 5 verbatim turns and working state in Redis."""

    def __init__(self, settings: CustomerHarnessSettings, provider, *, model: str) -> None:
        self.settings, self.provider, self.model = settings, provider, model
        self._client = None
        self._redis_retry_at = 0.0
        self._fallback: dict[str, dict[str, Any]] = {}
        self._summary_tasks: set[asyncio.Task] = set()
        self.handoff_service = CustomerHandoffService(
            settings.redis_url, settings.redis_prefix,
        )

    async def _redis(self):
        if self._client is not None:
            return self._client
        if not self.settings.redis_url:
            return None
        if time.monotonic() < self._redis_retry_at:
            return None
        try:
            import redis.asyncio as redis
            self._client = redis.from_url(
                self.settings.redis_url, decode_responses=True,
                socket_connect_timeout=1, socket_timeout=2,
            )
            await self._client.ping()
            return self._client
        except Exception:
            self._client = None
            self._redis_retry_at = time.monotonic() + 30
            return None

    def _key(self, session_key: str) -> str:
        return f"{self.settings.redis_prefix}:session:{session_key}"

    async def _read(self, session_key: str) -> dict[str, Any]:
        client = await self._redis()
        if client is not None:
            try:
                raw = await client.get(self._key(session_key))
                return json.loads(raw) if raw else {}
            except Exception:
                self._client = None
                self._redis_retry_at = time.monotonic() + 30
        return dict(self._fallback.get(session_key, {}))

    async def _write(self, session_key: str, value: dict[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        client = await self._redis()
        if client is not None:
            try:
                await client.set(
                    self._key(session_key), payload,
                    ex=self.settings.session_ttl_seconds,
                )
                return
            except Exception:
                self._client = None
                self._redis_retry_at = time.monotonic() + 30
        self._fallback[session_key] = json.loads(payload)

    async def load(self, session_key: str) -> ConversationContext:
        data = await self._read(session_key)
        turns = data.get("turns") if isinstance(data.get("turns"), list) else []
        recent = turns[-self.settings.recent_turns:]
        return ConversationContext(
            summary=str(data.get("summary") or ""),
            recent_turns=tuple(
                {"user": str(item.get("user") or ""),
                 "assistant": str(item.get("assistant") or "")}
                for item in recent if isinstance(item, dict)
            ),
            state=dict(data.get("state") or {}),
        )

    async def update_state(self, session_key: str, **updates: Any) -> None:
        data = await self._read(session_key)
        state = dict(data.get("state") or {})
        state.update(updates)
        data["state"] = state
        await self._write(session_key, data)

    async def append_turn(self, session_key: str, user: str, assistant: str) -> None:
        data = await self._read(session_key)
        turns = list(data.get("turns") or [])
        turns.append({"user": user[:12000], "assistant": assistant[:12000]})
        limit = self.settings.summary_turns + self.settings.recent_turns
        turns = turns[-limit:]
        older = turns[:-self.settings.recent_turns]
        data["turns"] = turns
        sequence = int(data.get("turn_sequence") or 0) + 1
        data["turn_sequence"] = sequence
        await self._write(session_key, data)
        if older:
            task = asyncio.create_task(
                self._refresh_summary(session_key, older, sequence),
                name=f"customer-summary:{session_key}",
            )
            self._summary_tasks.add(task)
            task.add_done_callback(self._summary_tasks.discard)

    async def _refresh_summary(
        self, session_key: str, turns: list[dict[str, str]], sequence: int,
    ) -> None:
        summary = await self._summarize(turns)
        if not summary:
            return
        data = await self._read(session_key)
        if int(data.get("turn_sequence") or 0) != sequence:
            return
        data["summary"] = summary
        await self._write(session_key, data)

    async def _summarize(self, turns: list[dict[str, str]]) -> str:
        payload = json.dumps(turns[-self.settings.summary_turns:], ensure_ascii=False)[:24000]
        prompt = (
            "Summarize these older customer-support turns. Preserve only confirmed customer "
            "facts, the exact product/model, troubleshooting already attempted, unresolved "
            "questions and promised next actions. Mark uncertainty; never convert a model guess "
            f"into a fact. Return only the concise summary.\n\n{payload}"
        )
        try:
            response = await asyncio.wait_for(
                self.provider.chat(
                    [{"role": "user", "content": prompt}],
                    tools=None, model=self.model,
                ),
                timeout=12,
            )
            if response.finish_reason != "error":
                return str(response.content or "").strip()[:6000]
        except Exception:
            pass
        return ""

    async def clear(self, session_key: str) -> None:
        task_name = f"customer-summary:{session_key}"
        for task in tuple(self._summary_tasks):
            if task.get_name() == task_name:
                task.cancel()
        self._fallback.pop(session_key, None)
        client = await self._redis()
        if client is not None:
            try:
                await client.delete(self._key(session_key))
            except Exception:
                self._client = None
                self._redis_retry_at = time.monotonic() + 30

    async def get_runtime(self, name: str) -> dict[str, Any]:
        key = f"{self.settings.redis_prefix}:runtime:{name}"
        client = await self._redis()
        if client is not None:
            try:
                raw = await client.get(key)
                return json.loads(raw) if raw else {}
            except Exception:
                self._client = None
                self._redis_retry_at = time.monotonic() + 30
        return dict(self._fallback.get(key, {}))

    async def set_runtime(self, name: str, value: dict[str, Any], ttl: int) -> None:
        key = f"{self.settings.redis_prefix}:runtime:{name}"
        client = await self._redis()
        if client is not None:
            try:
                await client.set(key, json.dumps(value), ex=max(1, ttl))
                return
            except Exception:
                self._client = None
                self._redis_retry_at = time.monotonic() + 30
        self._fallback[key] = dict(value)

    async def enqueue_handoff(self, record: dict[str, Any]) -> dict[str, Any]:
        return await self.handoff_service.create(record)


@dataclass
class CustomerLongTermMemory:
    """Read activated MySQL memories and store only explicit 'remember' requests."""

    service: CustomerMemoryService
    search_backend: Any | None = None

    async def recall(self, tenant_id: str, account_id: str, query: str) -> tuple[str, ...]:
        if not tenant_id or not account_id:
            return ()
        try:
            if self.search_backend is not None:
                actor = self.service.actor(tenant_id, account_id)
                memory_scope = MemoryScope(
                    "customer_private", tenant_id, account_id=account_id,
                    purpose="customer_support",
                )
                hits = await asyncio.to_thread(
                    self.search_backend.search, actor, memory_scope, query, 3,
                )
            else:
                hits = await asyncio.to_thread(
                    self.service.store.search_owned,
                    tenant_id=tenant_id, account_id=account_id,
                    conversation_id=None, purpose="customer_support",
                    query=query, top_k=3,
                )
            return tuple(hit.item.summary for hit in hits if hit.item.status == "active")
        except Exception:
            return ()

    async def remember_explicit(
        self, tenant_id: str, account_id: str, conversation_id: str,
        request_id: str, fact: str,
    ) -> bool:
        fact = fact.strip()[:1000]
        if not all((tenant_id, account_id, conversation_id, request_id, fact)):
            return False
        actor = self.service.actor(tenant_id, account_id)
        try:
            consent = await asyncio.to_thread(
                self.service.grant_consent, actor,
                purpose="customer_support", categories=("semantic",), expires_at=None,
            )
            candidate = await asyncio.to_thread(
                self.service.create_candidate, actor,
                content=fact, summary=fact[:300], memory_type="semantic",
                purpose="customer_support", source_refs=(request_id,),
                conversation_id=None, confidence=1.0, importance=0.7,
            )
            await asyncio.to_thread(
                self.service.activate_candidate, actor, candidate.memory_id,
                consent.consent_record_id, candidate.version,
            )
            return True
        except Exception:
            return False
