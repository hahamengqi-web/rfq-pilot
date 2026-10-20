import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from trade_rag.config import RemoteApiConfig
from trade_rag.contracts import HistoryMessage
from trade_rag.query_rewriter import (
    Qwen3QueryRewriteClient, QueryRewriter, create_application_query_rewriter,
)


class QueryRewriterTests(unittest.TestCase):
    def test_qwen3_request_uses_json_and_non_thinking_mode(self):
        class Response:
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content": '{"queries":["AX10 保修多久"]}'}}]}

        class Client:
            payload = None
            def post(self, url, *, headers, json, timeout):
                self.payload = json
                self.url = url
                return Response()

        client = Client()
        api = RemoteApiConfig("test-key", "https://example.invalid/v1", "Qwen/Qwen3-8B", 5)
        generate = Qwen3QueryRewriteClient(api, transfer_approved=True, client=client)
        rewritten = QueryRewriter(generate).rewrite(
            "它保修多久", (HistoryMessage("user", "AX10 有哪些功能"),),
        )
        self.assertEqual(rewritten, ["AX10 保修多久", "它保修多久"])
        self.assertEqual(client.url, "https://example.invalid/v1/chat/completions")
        self.assertEqual(client.payload["model"], "Qwen/Qwen3-8B")
        self.assertFalse(client.payload["enable_thinking"])
        self.assertEqual(client.payload["response_format"], {"type": "json_object"})

    def test_missing_approval_or_key_falls_back_without_network(self):
        api = RemoteApiConfig("", "https://example.invalid/v1", "Qwen/Qwen3-8B", 5)
        generate = Qwen3QueryRewriteClient(api, transfer_approved=False)
        rewriter = QueryRewriter(generate)
        self.assertEqual(rewriter.rewrite("原问题"), ["原问题"])
        self.assertEqual(rewriter.last_status, "fallback")

    def test_application_factory_selects_qwen3_without_calling_it(self):
        values = {
            "RAG_QUERY_REWRITE_PROVIDER": "qwen3",
            "RAG_QUERY_REWRITE_BASE_URL": "https://example.invalid/v1",
            "RAG_QUERY_REWRITE_MODEL": "Qwen/Qwen3-8B",
            "RAG_REMOTE_DATA_TRANSFER_APPROVED": "false",
        }
        with patch("trade_rag.config._PROJECT_ENV", Path("missing.env")), patch.dict(os.environ, values, clear=True):
            rewriter = create_application_query_rewriter()
        self.assertIsInstance(rewriter.generate, Qwen3QueryRewriteClient)
        self.assertEqual(rewriter.num_queries, 3)

    def test_history_window_prompt_and_deduplication(self):
        prompts = []
        def generate(prompt):
            prompts.append(prompt)
            return json.dumps({"queries": ["DAG Runtime 如何实现", "dag runtime 如何实现", "图式运行时实现方法"]}, ensure_ascii=False)
        history = tuple(HistoryMessage("user", str(i) * 250) for i in range(8))
        result = QueryRewriter(generate, 3).rewrite("那个怎么实现", history)
        self.assertEqual(result, ["DAG Runtime 如何实现", "图式运行时实现方法", "那个怎么实现"])
        self.assertNotIn("0" * 20, prompts[0]); self.assertIn("2" * 200, prompts[0]); self.assertNotIn("2" * 201, prompts[0])

    def test_json_fence_and_fallbacks(self):
        fenced = QueryRewriter(lambda _: '```json\n{"queries":["混合检索实现"]}\n```', 3)
        self.assertEqual(fenced.rewrite("怎么实现"), ["混合检索实现", "怎么实现"])
        self.assertEqual(QueryRewriter(lambda _: "bad json").rewrite("原问题"), ["原问题"])
        self.assertEqual(QueryRewriter().rewrite("原问题"), ["原问题"])
        self.assertEqual(QueryRewriter().rewrite("  "), [])

    def test_invalid_structure_and_long_item(self):
        generate = lambda _: json.dumps({"queries": ["x" * 51, "有效查询"]}, ensure_ascii=False)
        self.assertEqual(QueryRewriter(generate).rewrite("原始问题"), ["有效查询", "原始问题"])
        self.assertEqual(QueryRewriter(lambda _: '{"queries":"bad"}').rewrite("原问题"), ["原问题"])


if __name__ == "__main__": unittest.main()
