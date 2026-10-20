from __future__ import annotations
from dataclasses import replace
import inspect
from .contracts import Actor, SearchResult

class HybridRetriever:
    def __init__(self, semantic_store, keyword_store=None, embedder=None, semantic_weight: float = .7, rrf_k: int = 60):
        if not 0 <= semantic_weight <= 1: raise ValueError("semantic_weight must be between 0 and 1")
        if rrf_k <= 0: raise ValueError("rrf_k must be greater than 0")
        self.semantic_store, self.keyword_store, self.embedder = semantic_store, keyword_store, embedder
        self.semantic_weight, self.keyword_weight, self.rrf_k = semantic_weight, 1 - semantic_weight, rrf_k
        self.last_mode = "unavailable"

    @staticmethod
    def _search_store(store, value, actor, limit, allowed_document_ids):
        parameters = inspect.signature(store.search).parameters.values()
        supports_scope = (
            any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters)
            or any(parameter.name == "allowed_document_ids" for parameter in parameters)
        )
        if supports_scope:
            return store.search(
                value, actor, limit,
                allowed_document_ids=allowed_document_ids,
            )
        rows = store.search(value, actor, limit)
        allowed = set(allowed_document_ids)
        return [row for row in rows if not allowed or row.source.document_id in allowed]

    def search(self, query: str, actor: Actor, limit: int = 30,
               allowed_document_ids=()):
        semantic=[]; keyword=[]
        if self.semantic_store is not None and self.embedder is not None:
            try:
                vector = (self.embedder.embed_query(query) if hasattr(self.embedder, "embed_query")
                          else self.embedder.embed([query])[0])
                semantic = self._search_store(
                    self.semantic_store, vector, actor, limit, allowed_document_ids,
                )
            except Exception: semantic=[]
        if self.keyword_store is not None:
            try:
                keyword=self._search_store(
                    self.keyword_store, query, actor, limit, allowed_document_ids,
                )
            except Exception: keyword=[]
        if semantic and keyword: self.last_mode="hybrid"
        elif semantic: self.last_mode="semantic"
        elif keyword: self.last_mode="keyword"
        else: self.last_mode="unavailable"
        return self._fuse(semantic, keyword, limit)

    def _fuse(self, semantic, keyword, limit):
        rows={}; scores={}; relevance={}
        for weight, results in ((self.semantic_weight, semantic), (self.keyword_weight, keyword)):
            positive = [max(0.0, float(result.score)) for result in results]
            maximum = max(positive, default=0.0)
            for rank, result in enumerate(results, 1):
                key=result.child.child_id; rows.setdefault(key, result)
                scores[key]=scores.get(key, 0) + weight/(self.rrf_k+rank)
                if maximum > 0:
                    relevance[key]=relevance.get(key, 0.0) + weight*max(0.0, float(result.score))/maximum
        output=[]
        for key in sorted(rows, key=lambda k: (-scores[k], -rows[k].score)):
            output.append(replace(rows[key], score=relevance.get(key, 0.0),
                                  rrf_score=scores[key], retrieval_source=self.last_mode+"+rrf"))
        return output[:limit]
