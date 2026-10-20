from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Sequence

from .config import RemoteApiConfig, _load_project_env, _positive_float, _positive_int
from .contracts import HistoryMessage


class Qwen3QueryRewriteClient:
    """Use a non-thinking Qwen3 chat completion for retrieval query rewriting."""

    def __init__(self, api: RemoteApiConfig, *, transfer_approved: bool, client=None):
        self.api = api
        self.transfer_approved = transfer_approved
        self.client = client

    def __call__(self, prompt: str) -> str:
        if not self.transfer_approved:
            raise RuntimeError("rag_query_rewrite_remote_transfer_not_approved")
        if not self.api.configured:
            raise RuntimeError("rag_query_rewrite_api_not_configured")
        import httpx

        payload = {
            "model": self.api.model,
            "messages": [
                {"role": "system", "content": (
                    "你只负责把用户问题改写为检索查询，不回答问题。"
                    "历史消息只用于消除指代，只能使用当前问题或历史中明确出现的产品型号、参数和事实。"
                    "只能返回符合要求的 JSON。"
                )},
                {"role": "user", "content": prompt},
            ],
            "enable_thinking": False,
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 320,
        }
        try:
            http = self.client or httpx
            response = http.post(
                f"{self.api.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api.api_key}"},
                json=payload,
                timeout=self.api.timeout_seconds,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("query_rewrite_response_empty")
            return content
        except Exception as exc:
            raise RuntimeError("rag_query_rewrite_request_failed") from exc


class QueryRewriter:
    """历史感知多查询改写；生成失败时始终回退原始问题。"""

    def __init__(
        self,
        generate: Callable[[str], str] | None = None,
        num_queries: int = 3,
        *,
        max_history: int = 6,
        max_history_chars: int = 200,
        max_query_chars: int = 50,
        max_input_chars: int = 1000,
    ):
        self.generate = generate
        self.num_queries = num_queries if num_queries > 0 else 3
        self.max_history = max(0, max_history)
        self.max_history_chars = max(1, max_history_chars)
        self.max_query_chars = max(1, max_query_chars)
        self.max_input_chars = max(1, max_input_chars)
        self.last_status = "not_called"

    def rewrite(self, query: str, history: Sequence[HistoryMessage] = ()) -> list[str]:
        original = query.strip() if query else ""
        if not original:
            self.last_status = "skipped"
            return []
        if self.generate is None or self.num_queries <= 1:
            self.last_status = "skipped"
            return [original]
        try:
            parsed = self._parse(self.generate(self.build_prompt(original, history)))
        except Exception:
            self.last_status = "fallback"
            return [original]
        self.last_status = "success" if parsed else "fallback"
        candidates = parsed + [original]
        output, seen = [], set()
        for item in candidates:
            value = item.strip()
            key = value.casefold()
            if not value or len(value) > self.max_query_chars or key in seen:
                continue
            seen.add(key); output.append(value)
            if len(output) >= self.num_queries:
                break
        return output or [original]

    def build_prompt(self, query: str, history: Sequence[HistoryMessage]) -> str:
        recent = list(history)[-self.max_history :]
        lines = [f"[{m.role}] {m.content[:self.max_history_chars]}" for m in recent]
        history_text = "\n".join(lines) if lines else "（无历史，直接改写当前问题）"
        bounded_query = query[:self.max_input_chars]
        return (
            "你是查询改写器。历史只用于消除指代，不得改变意图或编造实体。\n"
            f"返回严格 JSON：{{\"queries\":[...]}}，共 {self.num_queries} 条；"
            f"第一条必须自包含，每条不超过 {self.max_query_chars} 字，不输出答案或解释。\n"
            f"最近对话历史：\n{history_text}\n当前问题：{bounded_query}"
        )

    def _parse(self, raw: str) -> list[str]:
        text = raw.strip()
        fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
        if fence:
            text = fence.group(1)
        payload = json.loads(text)
        values = payload.get("queries") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            raise ValueError("queries must be a list")
        return [value for value in values if isinstance(value, str)]


def create_application_query_rewriter() -> QueryRewriter:
    _load_project_env()
    provider = os.environ.get("RAG_QUERY_REWRITE_PROVIDER", "disabled").strip().lower()
    if provider == "disabled":
        return QueryRewriter()
    if provider != "qwen3":
        raise ValueError("RAG_QUERY_REWRITE_PROVIDER must be qwen3 or disabled")
    api = RemoteApiConfig(
        os.environ.get("RAG_QUERY_REWRITE_API_KEY", ""),
        os.environ.get("RAG_QUERY_REWRITE_BASE_URL", "").strip().rstrip("/"),
        os.environ.get("RAG_QUERY_REWRITE_MODEL", "").strip(),
        _positive_float("RAG_QUERY_REWRITE_TIMEOUT_SECONDS", 15),
    )
    approved = os.environ.get("RAG_REMOTE_DATA_TRANSFER_APPROVED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }
    generate = Qwen3QueryRewriteClient(api, transfer_approved=approved)
    return QueryRewriter(generate, num_queries=_positive_int("RAG_QUERY_REWRITE_NUM_QUERIES", 3))
