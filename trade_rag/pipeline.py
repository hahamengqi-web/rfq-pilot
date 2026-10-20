from __future__ import annotations
from .chunking import ParentChildSplitter
from dataclasses import replace
import inspect

from .contracts import CanonicalDocument, QueryRequest
from .embeddings import MockEmbeddingProvider, create_application_embedding_provider
from .generation import answer_with_citations, create_application_answer_generator
from .query_rewriter import QueryRewriter, create_application_query_rewriter
from .reranker import ListwiseReranker, create_application_reranker, unique_parent_results
from .retrieval import QualityGate, QueryRouter, reciprocal_rank_fusion
from .stores import InMemoryKeywordStore, InMemoryParentStore
from .keyword_runtime import create_application_keyword_store
from .vector_runtime import create_application_vector_store
from .parent_runtime import create_application_parent_store
from .hybrid import HybridRetriever
from .config import load_rag_vector_config

class RagPipeline:
    def __init__(self, *, embedder=None, store=None, keyword_store=None, parent_store=None, splitter=None, rewriter=None, reranker=None, generator=None, semantic_weight=.7, rrf_k=60):
        explicit_store = store is not None
        self._explicit_store = explicit_store
        self.generator = generator if generator is not None else (None if explicit_store else create_application_answer_generator())
        self.embedder = embedder if embedder is not None else (MockEmbeddingProvider() if explicit_store else create_application_embedding_provider())
        if not explicit_store and hasattr(self.embedder, "dimensions"):
            vector_dimensions = load_rag_vector_config().dimensions
            if self.embedder.dimensions != vector_dimensions:
                raise ValueError("rag_embedding_vector_dimensions_mismatch")
        self.store = store if store is not None else create_application_vector_store(model_id=self.embedder.model_id)
        self.keyword_store=keyword_store if keyword_store is not None else (InMemoryKeywordStore() if explicit_store else create_application_keyword_store()); self.parent_store=parent_store if parent_store is not None else (InMemoryParentStore() if explicit_store else create_application_parent_store()); self.splitter=splitter or ParentChildSplitter(); self.router=QueryRouter(); self.gate=QualityGate(); self.reranker=reranker if reranker is not None else (ListwiseReranker() if explicit_store else create_application_reranker()); self.rewriter=rewriter if rewriter is not None else (QueryRewriter() if explicit_store else create_application_query_rewriter()); self.retriever=HybridRetriever(self.store, self.keyword_store, self.embedder, semantic_weight, rrf_k)
    def index(self, document: CanonicalDocument):
        parents, children=self.splitter.split(document)
        return self.index_prepared(document, parents, children)
    def index_prepared(self, document, parents, children, *, progress_callback=None):
        parents=list(parents); children=list(children)
        if not parents or not children: raise ValueError("knowledge_chunks_empty")
        parent_ids={parent.parent_id for parent in parents}
        if any(child.parent_id not in parent_ids for child in children): raise ValueError("knowledge_parent_link_invalid")
        def progress(stage, completed, total):
            if progress_callback is not None:
                progress_callback(stage, int(completed), int(total))
        progress("embedding", 0, len(children))
        embed_parameters = inspect.signature(self.embedder.embed).parameters
        if "progress_callback" in embed_parameters:
            vectors=self.embedder.embed(
                [child.text for child in children],
                progress_callback=lambda completed, total: progress(
                    "embedding", completed, total,
                ),
            )
        else:
            vectors=self.embedder.embed([child.text for child in children])
            progress("embedding", len(children), len(children))
        progress("index_cleanup", 0, len(children))
        self.delete_by_document(document.document_id, document.version)
        # OCR details belong to the durable parsed artifact. Repeating every image's
        # OCR payload in each child row can turn one PDF into hundreds of megabytes
        # of duplicated index metadata and exceed backend request limits.
        source_metadata = dict(document.metadata)
        images = source_metadata.pop("images", None)
        if isinstance(images, list):
            source_metadata["image_count"] = len(images)
        indexed_document = replace(document, metadata=source_metadata)
        try:
            parent_count=self.parent_store.upsert(parents, indexed_document)
            progress("parent_index", parent_count, len(parents))
            semantic_count=self.store.upsert(children, indexed_document, vectors)
            progress("vector_index", semantic_count, len(children))
            keyword_count=self.keyword_store.upsert(children, indexed_document)
            progress("keyword_index", keyword_count, len(children))
            if parent_count != len(parents) or semantic_count != len(children) or keyword_count != len(children):
                raise RuntimeError("knowledge_index_count_mismatch")
        except Exception:
            self.delete_by_document(document.document_id, document.version)
            raise
        return {"document_id":document.document_id,"parents":parent_count,"children":len(children),"semantic_indexed":semantic_count,"keyword_indexed":keyword_count,"indexed_count":len(children),"model_id":self.embedder.model_id}
    def delete_by_document(self, document_id: str, version: int | None = None):
        deleted={"semantic":0,"keyword":0,"parents":0}; errors=[]
        for name, store in (("semantic", self.store), ("keyword", self.keyword_store), ("parents", self.parent_store)):
            try: deleted[name]=store.delete_by_document(document_id, version)
            except Exception as exc: errors.append(exc)
        if errors: raise RuntimeError("knowledge_index_delete_failed") from errors[0]
        semantic=deleted["semantic"]; keyword=deleted["keyword"]; parents=deleted["parents"]
        return {"document_id":document_id,"semantic_deleted":semantic,"keyword_deleted":keyword,"parents_deleted":parents}
    def query(self, request: QueryRequest):
        # Preserve the original query for embedding. Compatibility folding is
        # applied to parsed/indexed text and keyword matching; changing Unicode
        # punctuation here can alter embedding tokenization unnecessarily.
        request_query = request.query.strip()
        route = self.router.route(request_query)
        if route != "rag":
            route_responses = {
                "mysql": ("BUSINESS_DATA_REQUIRED", "该问题必须回查 MySQL 业务权威库。",
                          "authoritative_business_data_required", False),
                "calculator": ("CALCULATION_REQUIRED", "该问题必须使用确定性报价计算工具。",
                               "deterministic_calculator_required", False),
                "human_review": ("HUMAN_REVIEW_REQUIRED", "该操作涉及业务承诺，必须由授权人员确认。",
                                 "authorized_human_review_required", True),
            }
            response_status, answer, missing, human = route_responses[route]
            response = {"status": response_status, "answer": answer, "citations":[]}
            if request.evaluation_mode:
                response.update({
                    "claims": [], "assumptions": [],
                    "missing_information": [missing],
                    "requires_human_confirmation": human,
                    "_evaluation_trace": {
                        "schema_version": "rag-evaluation-trace-v1",
                        "query": request_query,
                        "route": route,
                        "rewritten_queries": [],
                        "candidates": [],
                        "selected_contexts": [],
                    },
                })
            return response
        queries=(self.rewriter.rewrite(request_query, request.history)
                 if request.rewrite else [request_query])
        pool=max(request.top_k * (4 if request.rerank else 2), 10)
        recall_limit=max(request.top_n, pool)
        allowed_document_ids = set(request.allowed_document_ids)
        result_sets=[]
        for query in queries:
            # Balance recall across the small allowlist. A 500-page manual must
            # not consume every candidate slot and hide an exact table in a
            # 2-page product sheet for the same model.
            scopes = ([{document_id} for document_id in sorted(allowed_document_ids)]
                      if allowed_document_ids else [set()])
            for scope in scopes:
                rows = self.retriever.search(
                    query, request.actor, recall_limit,
                    allowed_document_ids=scope,
                )
                if scope:
                    # Defense in depth for third-party adapters using the
                    # legacy search contract.
                    rows = [row for row in rows
                            if row.source.document_id in scope]
                result_sets.append(rows[:recall_limit])
        results=reciprocal_rank_fusion(result_sets, pool)
        primary=queries[0] if queries else request_query
        # RRF and backend relevance scores are relative to the current result
        # set, so even an unsupported question can receive a normalized top
        # score.  Rerank first, then let the quality gate inspect the absolute
        # Qwen3 relevance score.
        rerank_called = request.rerank and len(results) > 1
        if rerank_called:
            results = self.reranker.rerank(primary, results, len(results))
        status=self.gate.classify(results)
        results=unique_parent_results(results, request.top_k)
        expanded=[]
        for result in results:
            parent=self.parent_store.get(result.child.parent_id, request.actor)
            expanded.append(replace(result, parent=parent) if parent is not None else result)
        results=expanded
        if self.generator is not None:
            response = self.generator.answer(primary, results, status,
                                             include_claims=request.evaluation_mode)
        elif self._explicit_store:
            response = answer_with_citations(primary, results, status,
                                             include_claims=request.evaluation_mode)
        else:
            response = {"status": "GENERATION_UNAVAILABLE", "answer": "暂时无法核实产品资料，请稍后重试或转人工。",
                        "citations": [], "generation_status": "disabled"}
            if request.evaluation_mode:
                response.update({"claims": [], "assumptions": [],
                                 "missing_information": ["generation_disabled"],
                                 "requires_human_confirmation": True})
        response["query_count"] = len(queries)
        response["query_rewrite_status"] = (
            getattr(self.rewriter, "last_status", "unknown")
            if request.rewrite else "disabled"
        )
        response["rerank_status"] = (getattr(self.reranker, "last_status", "unknown")
                                     if rerank_called else "skipped" if request.rerank else "disabled")
        response["retrieval_mode"] = self.retriever.last_mode
        if request.evaluation_mode:
            selected_child_ids = {str(row.get("child_id", ""))
                                  for row in response.get("citations", [])}
            response["_evaluation_trace"] = {
                "schema_version": "rag-evaluation-trace-v1",
                "query": request_query,
                "route": route,
                "rewritten_queries": list(queries),
                "quality_status": status,
                "retrieval_mode": self.retriever.last_mode,
                "rerank_status": response["rerank_status"],
                "candidates": [self._evaluation_row(row) for row in
                               reciprocal_rank_fusion(result_sets, recall_limit)],
                "selected_contexts": [self._evaluation_row(row, include_text=True)
                                      for row in results
                                      if row.child.child_id in selected_child_ids],
            }
        return response

    @staticmethod
    def _evaluation_row(result, *, include_text: bool = True):
        row = {
            "document_id": result.source.document_id,
            "version": result.source.version,
            "child_id": result.child.child_id,
            "parent_id": result.child.parent_id,
            "location": result.child.location,
            "score": result.score,
            "rrf_score": result.rrf_score,
            "rerank_score": result.rerank_score,
            "retrieval_source": result.retrieval_source,
            "citation_id": ":".join((result.source.document_id,
                                      str(result.source.version),
                                      result.child.location,
                                      result.child.child_id)),
        }
        if include_text:
            row["text"] = (result.parent.text if result.parent is not None
                           else result.child.text)
        return row
