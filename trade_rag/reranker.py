from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Callable
from dataclasses import replace

from .contracts import SearchResult
from .config import RemoteApiConfig, load_rag_api_config


class ListwiseReranker:
    """一次 Listwise 调用完成排序；任何失败均返回原候选顺序。"""

    model_id = "listwise-json-v1"

    def __init__(self, generate: Callable[[str], str] | None = None, preview_len: int = 200):
        self.generate = generate
        self.preview_len = max(1, int(preview_len))
        self.last_status = "not_called"

    def rerank(self, query: str, results: list[SearchResult], top_k: int | None = None) -> list[SearchResult]:
        limit = max(0, top_k if top_k is not None else len(results))
        fallback = list(results[:limit])
        if not results or self.generate is None or len(results) == 1:
            self.last_status = "fallback"
            return fallback
        try:
            scores = self._parse(self.generate(self.build_prompt(query, results)), len(results))
        except Exception:
            self.last_status = "fallback"
            return fallback
        if not scores:
            self.last_status = "fallback"
            return fallback
        ranked = []
        for index, result in enumerate(results):
            value = scores.get(index, 0)
            original_score = result.rrf_score if result.rrf_score is not None else result.score
            ranked.append((value, original_score, -index, replace(result, score=value / 10.0, rrf_score=original_score, rerank_score=value / 10.0, retrieval_source=result.retrieval_source + "+rerank")))
        ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        self.last_status = "success"
        return [item[3] for item in ranked[:limit]]

    def build_prompt(self, query: str, results: list[SearchResult]) -> str:
        previews = []
        for index, result in enumerate(results):
            text = result.child.text
            preview = text if len(text) <= self.preview_len else text[: self.preview_len] + "..."
            previews.append(f"[{index}] {preview}")
        return (
            "仅依据候选段落评估与问题的相关性和信息密度，不得引入外部知识。\n"
            "返回严格 JSON：{\"scores\":[{\"idx\":0,\"score\":0}]}。"
            "每个候选恰好一次，idx 为候选下标，score 为 0 到 10 的整数。\n"
            f"用户问题：{query}\n候选段落：\n" + "\n".join(previews)
        )

    def _parse(self, raw: str, count: int) -> dict[int, int]:
        text = raw.strip()
        fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
        if fence:
            text = fence.group(1)
        payload = json.loads(text)
        items = payload.get("scores") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ValueError("scores must be a list")
        scores: dict[int, int] = {}
        for item in items:
            if not isinstance(item, dict): continue
            idx, score = item.get("idx"), item.get("score")
            if isinstance(idx, bool) or not isinstance(idx, int) or idx < 0 or idx >= count or idx in scores: continue
            if isinstance(score, bool) or not isinstance(score, int) or not math.isfinite(score) or not 0 <= score <= 10: continue
            scores[idx] = score
        return scores


class Qwen3Reranker:
    """Call the SiliconFlow rerank API and retain source-row identity."""

    def __init__(self, api: RemoteApiConfig, *, transfer_approved: bool,
                 max_candidates: int = 30, client=None):
        self.api = api
        self.transfer_approved = transfer_approved
        self.max_candidates = max(1, int(max_candidates))
        self.client = client
        self.model_id = api.model
        self.last_status = "not_called"

    def rerank(self, query: str, results: list[SearchResult],
               top_k: int | None = None) -> list[SearchResult]:
        limit = max(0, top_k if top_k is not None else len(results))
        fallback = list(results[:limit])
        if not results or limit == 0 or len(results) == 1:
            self.last_status = "skipped"
            return fallback
        if not self.transfer_approved:
            self.last_status = "remote_transfer_not_approved"
            return fallback
        if not self.api.configured:
            self.last_status = "api_not_configured"
            return fallback

        candidates = results[:self.max_candidates]
        payload = {
            "model": self.api.model,
            "query": query[:1000],
            "documents": [row.child.text[:2000] for row in candidates],
            "instruction": "根据产品咨询或售后问题，优先选择直接回答问题且包含明确产品型号、参数或操作条件的片段。",
            "top_n": len(candidates),
            "return_documents": False,
        }
        try:
            import httpx
            http = self.client or httpx
            response = http.post(
                f"{self.api.base_url}/rerank",
                headers={"Authorization": f"Bearer {self.api.api_key}"},
                json=payload, timeout=self.api.timeout_seconds,
            )
            response.raise_for_status()
            items = response.json()["results"]
        except Exception:
            self.last_status = "request_failed"
            return fallback

        scores: dict[int, float] = {}
        if not isinstance(items, list) or len(items) != len(candidates):
            self.last_status = "invalid_response"
            return fallback
        for item in items:
            if not isinstance(item, dict):
                self.last_status = "invalid_response"
                return fallback
            index = item.get("index")
            value = item.get("relevance_score")
            if (isinstance(index, bool) or not isinstance(index, int)
                    or index < 0 or index >= len(candidates) or index in scores
                    or isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value)):
                self.last_status = "invalid_response"
                return fallback
            scores[index] = float(value)
        if len(scores) != len(candidates):
            self.last_status = "invalid_response"
            return fallback

        ranked_indexes = sorted(scores, key=lambda index: (-scores[index], index))
        ranked = [replace(
            candidates[index], score=scores[index], rerank_score=scores[index],
            retrieval_source=candidates[index].retrieval_source + "+rerank",
        ) for index in ranked_indexes]
        self.last_status = "success_partial" if len(results) > len(candidates) else "success"
        return (ranked + list(results[len(candidates):]))[:limit]


def create_application_reranker():
    config = load_rag_api_config()
    provider = os.environ.get("RAG_RERANK_PROVIDER", "qwen3").strip().lower()
    if provider == "disabled":
        return ListwiseReranker()
    if provider != "qwen3":
        raise ValueError("RAG_RERANK_PROVIDER must be qwen3 or disabled")
    return Qwen3Reranker(
        config.rerank,
        transfer_approved=config.remote_data_transfer_approved,
        max_candidates=config.rerank_max_candidates,
    )


def unique_parent_results(results: list[SearchResult], top_k: int) -> list[SearchResult]:
    output, seen = [], set()
    for result in results:
        if result.child.parent_id in seen: continue
        seen.add(result.child.parent_id); output.append(result)
        if len(output) >= max(0, top_k): break
    return output
