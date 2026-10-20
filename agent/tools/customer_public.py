"""Allowlisted, read-only knowledge and product access for the public customer agent."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from typing import Any

from agent.customer_security import CustomerDataGuard
from agent.tools.base import Tool
from trade_rag.contracts import Actor, QueryRequest
from trade_rag.knowledge_repository import KnowledgeRepository
from trade_rag.pipeline import RagPipeline
from trade_rag.stores import InMemoryKeywordStore, InMemoryParentStore, InMemoryVectorStore


logger = logging.getLogger(__name__)


class CustomerPublicKnowledgeTool(Tool):
    def __init__(self, repository: KnowledgeRepository | None = None,
                 public_memory_store=None, pipeline: RagPipeline | None = None) -> None:
        explicit_repository = repository is not None
        self.repository = repository or KnowledgeRepository()
        self.public_memory_store = public_memory_store
        self.pipeline = pipeline
        if explicit_repository and pipeline is None:
            text_documents = [
                document for document in self.repository.load_published()
                if document.classification == "public"
            ]
            list_pdf_candidates = getattr(
                self.repository, "list_pdf_index_candidates", None
            )
            pdf_ids = ([
                document_id for document_id in list_pdf_candidates()
                if self.repository.get_document(document_id).get("classification") == "public"
            ] if callable(list_pdf_candidates) else [])
            if text_documents or pdf_ids:
                self.pipeline = RagPipeline(
                    store=InMemoryVectorStore(),
                    keyword_store=InMemoryKeywordStore(),
                    parent_store=InMemoryParentStore(),
                )
                for document in text_documents:
                    self.pipeline.index(document)
                for document_id in pdf_ids:
                    document, parents, children = self.repository.load_pdf_chunks(document_id)
                    self.pipeline.index_prepared(document, parents, children)

    @property
    def name(self) -> str:
        return "search_public_knowledge"

    @property
    def description(self) -> str:
        return "Search only knowledge documents explicitly classified as public. Use only to answer the customer's current question."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 500}},
            "required": ["query"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        query = str(kwargs.get("query", "")).strip()[:500]
        fast = bool(kwargs.get("fast", False))
        product_model = str(kwargs.get("product_model", "")).strip()[:80]
        required_types = {
            self._canonical_document_type(value)
            for value in kwargs.get("document_types", ())
            if str(value).strip()
        }
        if not query:
            return json.dumps({"status": "NO_EVIDENCE", "answer": ""})
        if self.public_memory_store is not None:
            approved = self.public_memory_store.search(query, top_k=3)
            if approved:
                answer = "\n".join(str(item).strip() for item in approved if str(item).strip())
                if (answer and not CustomerDataGuard.inspect_request(answer)
                        and CustomerDataGuard.sanitize_response(answer) == answer):
                    return json.dumps({
                        "status": "ANSWERED", "answer": answer, "citations": [],
                        "matched_documents": [], "public_memory_ids": [],
                    }, ensure_ascii=False)
        matched_documents = (
            self.public_documents_for_model(product_model)
            if product_model else self.public_documents_by_type(required_types)
            if required_types else []
        )
        eligible_documents = [
            item for item in matched_documents
            if not required_types or self._canonical_document_type(
                item.get("document_type", "")
            ) in required_types
        ]
        # A model-scoped question must never fall through to another product.
        if product_model and not matched_documents:
            return json.dumps({
                "status": "NO_EVIDENCE", "answer": "", "citations": [],
                "matched_documents": [],
            }, ensure_ascii=False)
        if required_types and not eligible_documents:
            return json.dumps({
                "status": "NO_EVIDENCE", "answer": "", "citations": [],
                "matched_documents": matched_documents,
                "missing_document_types": sorted(required_types),
            }, ensure_ascii=False)
        try:
            if self.pipeline is None:
                self.pipeline = RagPipeline()
            result = await asyncio.to_thread(self.pipeline.query, QueryRequest(
                query=query,
                actor=Actor(actor_id="customer-agent", roles=frozenset({"customer"})),
                top_k=5,
                rewrite=not fast,
                # Short, model-specific questions can skip query rewriting, but
                # still need reranking.  Vector-only retrieval missed exact
                # specification rows such as the Archer AX55 port count.
                rerank=True,
                allowed_document_ids=tuple(
                    item["document_id"] for item in eligible_documents
                ),
            ))
        except Exception as exc:
            logger.exception(
                "Public knowledge query failed (error_type=%s)",
                type(exc).__name__,
            )
            return json.dumps({
                "status": "TEMPORARILY_UNAVAILABLE", "answer": "",
                "citations": [],
            }, ensure_ascii=False)
        citations = []
        for item in result.get("citations", []):
            if not isinstance(item, dict):
                return json.dumps({"status": "NO_EVIDENCE", "answer": "", "citations": []})
            try:
                detail = self.repository.get_document(str(item.get("document_id") or ""))
                if (detail.get("status") != "published" or detail.get("classification") != "public"
                        or int(detail.get("version") or 0) != int(item.get("version") or -1)):
                    raise ValueError("citation_not_public")
            except Exception:
                return json.dumps({"status": "NO_EVIDENCE", "answer": "", "citations": []})
            excerpt = str(item.get("excerpt") or "").strip()[:600]
            citations.append({
                "document_id": item["document_id"],
                "version": item["version"],
                "title": str(
                    item.get("title") or detail.get("title")
                    or detail.get("original_name") or "产品资料"
                )[:200],
                "location": str(item.get("location") or "")[:200],
                "page_number": item.get("page_number"),
                "excerpt": excerpt,
            })
        answer = str(result.get("answer") or "") if citations and result.get("status") == "ANSWERED" else ""
        if CustomerDataGuard.inspect_request(answer) or CustomerDataGuard.sanitize_response(answer) != answer:
            return json.dumps({"status": "NO_EVIDENCE", "answer": ""})
        return json.dumps({
            "status": "ANSWERED" if answer else result.get("status", "NO_EVIDENCE"),
            "answer": answer,
            "citations": citations,
            "matched_documents": matched_documents,
            "public_memory_ids": [],
        }, ensure_ascii=False)

    @staticmethod
    def _normalized_model(value: str) -> str:
        value = unicodedata.normalize("NFKC", str(value or ""))
        return re.sub(r"[^a-z0-9]", "", value.casefold())

    @staticmethod
    def _canonical_document_type(value: str) -> str:
        aliases = {
            "datasheet": "product_info",
            "product_document": "product_info",
            "after_sales_guide": "maintenance_manual",
        }
        normalized = str(value or "").strip().casefold()
        return aliases.get(normalized, normalized)

    def _public_document_details(self) -> list[dict[str, Any]]:
        try:
            items, _, _ = self.repository.list_documents(status="published", limit=100)
        except Exception:
            return []
        details: list[dict[str, Any]] = []
        for item in items:
            if item.get("classification") != "public":
                continue
            try:
                detail = self.repository.get_document(str(item.get("document_id") or ""))
            except Exception:
                continue
            if (detail.get("status") != "published"
                    or detail.get("classification") != "public"):
                continue
            details.append(detail)
        return details

    @staticmethod
    def _document_summary(detail: dict[str, Any]) -> dict[str, Any]:
        return {
            "document_id": str(detail.get("document_id") or ""),
            "title": str(detail.get("title") or detail.get("original_name") or "产品资料")[:200],
            "version": int(detail.get("version") or 1),
            "document_type": str(detail.get("document_type") or "document")[:60],
            "document_version": str(detail.get("document_version") or detail.get("version") or "")[:40],
            "product_models": [str(value)[:80] for value in detail.get("product_models") or ()],
        }

    @classmethod
    def _model_matches(cls, requested: str, candidate: str) -> bool:
        return bool(
            requested and candidate and len(candidate) >= 4
            and (requested == candidate or requested in candidate or candidate in requested)
        )

    def _model_seed_documents(
        self, product_model: str, details: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        requested = self._normalized_model(product_model)
        if len(requested) < 4:
            return []
        matches: list[dict[str, Any]] = []
        for detail in details:
            declared = [
                self._normalized_model(value)
                for value in detail.get("product_models") or ()
            ]
            title_model = self._normalized_model(detail.get("title") or "")
            if (any(self._model_matches(requested, value) for value in declared)
                    or requested in title_model):
                matches.append(detail)
        return matches

    def canonical_product_model(self, product_model: str) -> str:
        """Resolve a marketing alias to one unambiguous declared model."""
        value = str(product_model or "").strip()[:80]
        details = self._public_document_details()
        seeds = self._model_seed_documents(value, details)
        declared: dict[str, str] = {}
        for detail in seeds:
            for model in detail.get("product_models") or ():
                normalized = self._normalized_model(model)
                if len(normalized) >= 4:
                    declared.setdefault(normalized, str(model).strip()[:80])
        return next(iter(declared.values())) if len(declared) == 1 else value

    def public_documents_for_model(self, product_model: str) -> list[dict[str, Any]]:
        """Return public documents for a declared model or marketing alias.

        A product-info title often contains the retail family name while every
        manual is tagged with the regulatory model.  Seed on either form, then
        expand through the document's declared model so those manuals remain in
        the retrieval scope.
        """
        details = self._public_document_details()
        seeds = self._model_seed_documents(product_model, details)
        if not seeds:
            return []
        aliases = {
            normalized
            for detail in seeds
            for value in detail.get("product_models") or ()
            if len(normalized := self._normalized_model(value)) >= 4
        }
        requested = self._normalized_model(product_model)
        matches: list[dict[str, Any]] = []
        for detail in details:
            declared = {
                self._normalized_model(value)
                for value in detail.get("product_models") or ()
            }
            title = self._normalized_model(detail.get("title") or "")
            if (detail in seeds or declared.intersection(aliases)
                    or any(alias in title for alias in aliases)
                    or requested in title):
                matches.append(self._document_summary(detail))
        return matches

    def public_documents_by_type(self, required_types: set[str]) -> list[dict[str, Any]]:
        """Bound model-free policy questions to public documents of the requested type."""
        if not required_types:
            return []
        try:
            items, _, _ = self.repository.list_documents(status="published", limit=100)
        except Exception:
            return []
        matches: list[dict[str, Any]] = []
        for item in items:
            if item.get("classification") != "public":
                continue
            try:
                detail = self.repository.get_document(str(item.get("document_id") or ""))
            except Exception:
                continue
            if self._canonical_document_type(detail.get("document_type", "")) not in required_types:
                continue
            if detail.get("product_models"):
                # A policy tagged to a specific model is not a general policy.
                continue
            matches.append({
                "document_id": str(detail.get("document_id") or ""),
                "title": str(detail.get("title") or detail.get("original_name") or "产品资料")[:200],
                "version": int(detail.get("version") or 1),
                "document_type": str(detail.get("document_type") or "document")[:60],
                "document_version": str(detail.get("document_version") or detail.get("version") or "")[:40],
                "product_models": [str(value)[:80] for value in detail.get("product_models") or ()],
            })
        return matches

    def public_manuals(self, limit: int = 12) -> list[dict[str, Any]]:
        """List public manuals with their declared model and version."""
        try:
            items, _, _ = self.repository.list_documents(
                status="published", limit=max(1, min(int(limit), 30)),
            )
        except Exception:
            return []
        manuals: list[dict[str, Any]] = []
        for item in items:
            if item.get("classification") != "public":
                continue
            try:
                detail = self.repository.get_document(str(item.get("document_id") or ""))
            except Exception:
                continue
            doc_type = str(detail.get("document_type") or "").casefold()
            title = str(detail.get("title") or detail.get("original_name") or "")
            if "manual" not in doc_type and not re.search(
                r"手册|说明书|manual|user guide", title, re.I,
            ):
                continue
            manuals.append({
                "document_id": str(detail.get("document_id") or ""),
                "title": title[:200],
                "version": int(detail.get("version") or 1),
                "document_version": str(detail.get("document_version") or detail.get("version") or "")[:40],
                "product_models": [str(value)[:80] for value in detail.get("product_models") or ()],
            })
        return manuals

    def public_document_titles(self, limit: int = 12) -> list[str]:
        """Return public, published knowledge titles without exposing source paths."""
        try:
            items, _, _ = self.repository.list_documents(
                status="published", limit=max(1, min(int(limit), 30)),
            )
        except Exception:
            return []
        titles = []
        for item in items:
            if item.get("classification") != "public":
                continue
            title = str(item.get("title") or item.get("original_name") or "").strip()
            if title and title not in titles:
                titles.append(title[:160])
        return titles


class CustomerPublicCatalogTool(Tool):
    @property
    def name(self) -> str:
        return "search_public_product_catalog"

    @property
    def description(self) -> str:
        return "Read allowlisted public product fields. Never returns raw SQL, exact stock, internal prices, costs, customers, quotes, or operational records."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "maxLength": 120},
                "sku": {"type": "string", "maxLength": 60},
                "category": {"type": "string", "maxLength": 60},
                "quantity": {"type": "integer", "minimum": 1, "maximum": 100000000},
            },
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        from agent.business.database import check_inventory, get_product_by_sku, list_products

        keyword = str(kwargs.get("keyword", "")).strip()[:120]
        sku = str(kwargs.get("sku", "")).strip()[:60]
        category = str(kwargs.get("category", "")).strip()[:60]
        quantity = kwargs.get("quantity")
        try:
            products = ([get_product_by_sku(sku)] if sku else
                        list_products(category=category, keyword=keyword, limit=5))
            rows = []
            for product in products:
                if product is None or not product.active:
                    continue
                row: dict[str, Any] = {
                    "sku": product.sku,
                    "name": product.name_en or product.name_cn,
                    "category": product.category,
                    "specification": product.specification,
                    "unit": product.unit,
                    "moq": product.moq,
                    "lead_time_days_estimate": product.lead_time_days,
                    "price_status": "sales_confirmation_required",
                }
                if quantity is not None:
                    availability = check_inventory(product.sku, int(quantity))
                    row["requested_quantity_available"] = bool(availability.get("available"))
                rows.append(row)
            payload = json.dumps({"results": rows, "total": len(rows)}, ensure_ascii=False)
            if (CustomerDataGuard.inspect_request(payload) or
                    CustomerDataGuard.sanitize_response(payload) != payload):
                return json.dumps({"results": [], "total": 0, "status": "temporarily_unavailable"})
            return payload
        except Exception:
            return json.dumps({"results": [], "total": 0, "status": "temporarily_unavailable"})
