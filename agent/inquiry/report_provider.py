"""Structured OpenAI-compatible provider for due-diligence reports."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from openai import AsyncOpenAI

from privacy import sanitize_text
from providers.base import LLMProvider, LLMResponse


class StructuredReportProvider(LLMProvider):
    """Generate bounded JSON reports with retry and usage accounting."""

    RETRYABLE_FINISH_REASONS = {"insufficient_system_resource", "aborted"}

    def __init__(self, *, api_key: str, base_url: str, model: str,
                 max_tokens: int = 8192, timeout_seconds: float = 180,
                 max_retries: int = 2, reasoning_effort: str | None = None,
                 fallback_model: str | None = "deepseek-flash",
                 fallback_max_retries: int = 1):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.reasoning_effort = reasoning_effort
        self.fallback_model = fallback_model
        self.fallback_max_retries = fallback_max_retries
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
        )

    async def chat(self, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None = None,
                   model: str | None = None) -> LLMResponse:
        if tools:
            return LLMResponse(content="报告生成接口不允许调用工具", finish_reason="error")

        primary_model = model or self.model
        response, retry_exhausted = await self._chat_model(
            messages, primary_model, self.max_retries, fallback_used=False)
        if (retry_exhausted and self.fallback_model
                and self.fallback_model != primary_model):
            response, _ = await self._chat_model(
                messages, self.fallback_model, self.fallback_max_retries,
                fallback_used=True)
        return response

    async def _chat_model(self, messages: list[dict[str, Any]], model: str,
                          max_retries: int, *, fallback_used: bool):
        """Return (response, retry_exhausted)."""

        last_error = "report_model_request_failed"
        for attempt in range(max_retries + 1):
            try:
                request: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "response_format": {"type": "json_object"},
                    "max_tokens": self.max_tokens,
                }
                if self.reasoning_effort == "none":
                    request["extra_body"] = {"thinking": {"type": "disabled"}}
                elif self.reasoning_effort:
                    request["reasoning_effort"] = self.reasoning_effort
                    request["extra_body"] = {"thinking": {"type": "enabled"}}

                response = await self.client.chat.completions.create(**request)
                choice = response.choices[0]
                finish_reason = choice.finish_reason or "stop"
                usage = {}
                if response.usage:
                    usage = {
                        "prompt_tokens": response.usage.prompt_tokens,
                        "completion_tokens": response.usage.completion_tokens,
                        "total_tokens": response.usage.total_tokens,
                    }
                usage.update({"model": model, "fallback_used": fallback_used})
                result = LLMResponse(
                    content=choice.message.content,
                    finish_reason=finish_reason,
                    usage=usage,
                )
                if (finish_reason not in self.RETRYABLE_FINISH_REASONS
                        or attempt >= max_retries):
                    return result, finish_reason in self.RETRYABLE_FINISH_REASONS
                last_error = f"report_model_{finish_reason}"
            except Exception as exc:
                last_error = sanitize_text(f"report_model_request_failed: {exc}")
                status_code = getattr(exc, "status_code", None)
                if status_code and status_code not in {408, 409, 429} and status_code < 500:
                    return LLMResponse(
                        content=json.dumps({"error": last_error}, ensure_ascii=False),
                        finish_reason="error",
                        usage={"model": model, "fallback_used": fallback_used},
                    ), False

            if attempt < max_retries:
                await asyncio.sleep(2 ** attempt)

        return LLMResponse(
            content=json.dumps({"error": last_error}, ensure_ascii=False),
            finish_reason="error",
            usage={"model": model, "fallback_used": fallback_used},
        ), True
