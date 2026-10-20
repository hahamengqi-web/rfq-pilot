"""Timeout, exponential retry and Redis-backed circuit breaking."""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .memory import RedisConversationMemory
from .settings import CustomerHarnessSettings


class CircuitOpen(RuntimeError):
    pass


class ReliableToolExecutor:
    def __init__(self, memory: RedisConversationMemory,
                 settings: CustomerHarnessSettings) -> None:
        self.memory, self.settings = memory, settings

    async def _before(self, tool_name: str) -> None:
        state = await self.memory.get_runtime(f"circuit:{tool_name}")
        if float(state.get("open_until") or 0) > time.time():
            raise CircuitOpen(tool_name)

    async def _record(self, tool_name: str, failed: bool) -> None:
        state = await self.memory.get_runtime(f"circuit:{tool_name}")
        outcomes = [int(item) for item in state.get("outcomes") or ()]
        outcomes.append(1 if failed else 0)
        outcomes = outcomes[-self.settings.circuit_window:]
        consecutive = int(state.get("consecutive") or 0) + 1 if failed else 0
        opened = (
            consecutive >= self.settings.circuit_consecutive_failures
            or (len(outcomes) >= self.settings.circuit_window
                and sum(outcomes) >= self.settings.circuit_failure_threshold)
        )
        await self.memory.set_runtime(
            f"circuit:{tool_name}",
            {"outcomes": outcomes, "consecutive": consecutive,
             "open_until": time.time() + self.settings.circuit_cooldown_seconds if opened else 0},
            max(self.settings.circuit_cooldown_seconds * 2, 120),
        )

    async def execute_json(
        self, tool_name: str, call: Callable[[], Awaitable[str]],
    ) -> dict[str, Any]:
        await self._before(tool_name)
        last_error: Exception | None = None
        for attempt in range(self.settings.tool_max_attempts):
            try:
                raw = await asyncio.wait_for(call(), timeout=self.settings.tool_timeout_seconds)
                data = json.loads(raw)
                if data.get("status") == "TEMPORARILY_UNAVAILABLE":
                    raise RuntimeError("tool_temporarily_unavailable")
                await self._record(tool_name, False)
                return data
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt + 1 >= self.settings.tool_max_attempts:
                    break
                delay = self.settings.backoff_base_seconds * (2 ** attempt)
                delay *= random.uniform(.8, 1.2)
                await asyncio.sleep(delay)
        await self._record(tool_name, True)
        raise RuntimeError("tool_retry_exhausted") from last_error
