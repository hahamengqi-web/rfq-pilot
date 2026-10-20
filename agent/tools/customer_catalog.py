"""Public structured product catalog for deterministic customer recommendations."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agent.tools.base import Tool


class CustomerStructuredCatalogTool(Tool):
    """Search explicitly public product attributes without exposing commercial data."""

    def __init__(self, catalog_path: str | Path | None = None) -> None:
        self.catalog_path = Path(catalog_path or (
            Path(__file__).resolve().parents[2] / "data" / "public_product_catalog.json"
        ))

    @property
    def name(self) -> str:
        return "search_public_product_catalog"

    @property
    def description(self) -> str:
        return "Recommend public products by model, type, color, features and usage scenarios."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 500}},
            "required": ["query"],
            "additionalProperties": False,
        }

    def _products(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        rows = raw.get("products") if isinstance(raw, dict) else None
        return [dict(row) for row in rows or [] if isinstance(row, dict) and row.get("public")]

    @staticmethod
    def _terms(product: dict[str, Any]) -> list[str]:
        values: list[str] = []
        for key in ("model", "name", "type"):
            values.append(str(product.get(key) or ""))
        for key in ("aliases", "colors", "features", "scenarios"):
            values.extend(str(item) for item in product.get(key) or ())
        return [item.strip().casefold() for item in values if item.strip()]

    @staticmethod
    def _query_terms(query: str) -> set[str]:
        query = query.casefold()
        aliases = {
            "router": ("路由器", "router", "wlan-router"),
            "black": ("黑色", "black", "schwarz"),
            "home": ("家庭", "家用", "home", "zuhause"),
            "gaming": ("游戏", "电竞", "gaming", "spiele"),
            "streaming": ("视频", "影音", "4k", "streaming"),
            "smart home": ("智能家居", "iot", "smart home", "smarthome"),
            "wifi 6": ("wi-fi 6", "wifi 6", "无线6", "802.11ax"),
            "usb": ("usb", "存储共享", "打印共享"),
        }
        found: set[str] = set()
        for canonical, variants in aliases.items():
            if any(variant in query for variant in variants):
                found.add(canonical)
        found.update(token for token in re.split(r"[^\w.-]+", query) if len(token) >= 2)
        return found

    def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        normalized = str(query or "").strip().casefold()[:500]
        wanted = self._query_terms(normalized)
        generic = any(term in normalized for term in (
            "推荐", "产品", "目录", "有哪些", "recommend", "catalog", "produkte",
        ))
        scored: list[tuple[int, dict[str, Any]]] = []
        for product in self._products():
            terms = self._terms(product)
            model = str(product.get("model") or "").casefold()
            score = 12 if model and model in normalized else 0
            for wanted_term in wanted:
                if any(wanted_term in term or term in wanted_term for term in terms):
                    score += 3
            if score == 0 and generic:
                score = 1
            if score:
                scored.append((score, product))
        scored.sort(key=lambda item: (-item[0], str(item[1].get("model") or "")))
        return [row for _, row in scored[:max(1, min(int(limit), 10))]]

    async def execute(self, **kwargs: Any) -> str:
        products = self.search(str(kwargs.get("query") or ""), limit=5)
        public = []
        for product in products:
            public.append({
                key: product.get(key) for key in (
                    "sku", "model", "name", "type", "colors", "features", "scenarios",
                    "source_document_id", "source_version", "source_title",
                    "source_page", "source_excerpt",
                )
            })
        return json.dumps({"status": "FOUND" if public else "NO_MATCH", "products": public}, ensure_ascii=False)
