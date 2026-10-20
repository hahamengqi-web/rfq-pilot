import os
import unittest
from unittest.mock import patch

from trade_rag.config import RagApiConfig, RemoteApiConfig
from trade_rag.embeddings import Qwen3EmbeddingProvider


class _Response:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": self.data}


class _Client:
    def __init__(self):
        self.calls = []

    def post(self, url, *, headers, json, timeout):
        self.calls.append((url, headers, json, timeout))
        rows = [
            {"index": index, "embedding": [float(index + 1)] * json["dimensions"]}
            for index in range(len(json["input"]))
        ]
        return _Response(list(reversed(rows)))


def _config(approved=True):
    return RagApiConfig(
        embedding=RemoteApiConfig("test-key", "https://example.invalid/v1", "Qwen/Qwen3-Embedding-8B", 5),
        embedding_dimensions=1024,
        embedding_batch_size=2,
        rerank=RemoteApiConfig("", "", "", 5),
        rerank_max_candidates=30,
        remote_data_transfer_approved=approved,
    )


class Qwen3EmbeddingTests(unittest.TestCase):
    def setUp(self):
        # Network-contract tests must exercise the injected client instead of
        # inheriting artifacts from the application's persistent cache.
        self.cache_env = patch.dict(
            os.environ, {"RAG_EMBEDDING_CACHE_ENABLED": "false"}
        )
        self.cache_env.start()

    def tearDown(self):
        self.cache_env.stop()

    def test_batches_and_orders_1024_dimensional_vectors(self):
        client = _Client()
        embedder = Qwen3EmbeddingProvider(_config(), client=client)
        vectors = embedder.embed(["产品 A", "产品 B", "产品 C"])
        self.assertEqual([len(vector) for vector in vectors], [1024, 1024, 1024])
        self.assertEqual([vector[0] for vector in vectors], [1.0, 2.0, 1.0])
        self.assertEqual([call[2]["input"] for call in client.calls], [["产品 A", "产品 B"], ["产品 C"]])
        self.assertTrue(all(call[2]["dimensions"] == 1024 for call in client.calls))

    def test_query_uses_retrieval_instruction(self):
        client = _Client()
        embedder = Qwen3EmbeddingProvider(_config(), client=client)
        self.assertEqual(len(embedder.embed_query("保修多久")), 1024)
        self.assertIn("Query: 保修多久", client.calls[0][2]["input"][0])

    def test_transfer_gate_prevents_request(self):
        client = _Client()
        embedder = Qwen3EmbeddingProvider(_config(approved=False), client=client)
        with self.assertRaisesRegex(RuntimeError, "rag_embedding_remote_transfer_not_approved"):
            embedder.embed(["产品 A"])
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
