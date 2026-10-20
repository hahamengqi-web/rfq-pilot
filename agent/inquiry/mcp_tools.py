"""Typed adapters from the inquiry workflow to its MCP tool server."""
from __future__ import annotations

import json

from .repository import InquiryError


SERVER = "inquiry_workflow_tools"


class InquiryMCPGateway:
    def __init__(self, manager, server_name: str = SERVER):
        self.manager, self.server_name = manager, server_name

    @property
    def available(self) -> bool:
        prefix = self.server_name + "__"
        return any(getattr(tool, "name", "").startswith(prefix) for tool in self.manager.get_tools())

    async def call(self, tool: str, **arguments):
        raw = await self.manager.call_tool(self.server_name, tool, arguments)
        if str(raw).startswith("错误:"):
            raise InquiryError("inquiry_mcp_unavailable", 503)
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise InquiryError("inquiry_mcp_invalid_response", 502) from exc
        if isinstance(value, dict) and value.get("status") == "error":
            raise InquiryError(str(value.get("error") or "inquiry_mcp_tool_failed"), 422)
        return value


class MCPResearchWeb:
    def __init__(self, gateway):
        self.gateway = gateway

    async def official_website(self, query):
        return await self.gateway.call("research_official_website", query=query)

    async def map_lookup(self, query):
        return await self.gateway.call("research_map_lookup", query=query)

    async def customs_lookup(self, query):
        return await self.gateway.call("research_customs_lookup", query=query)

    async def news_search(self, query):
        return await self.gateway.call("research_news_search", query=query)


class MCPCatalog:
    def __init__(self, gateway):
        self.gateway = gateway

    async def search(self, keyword):
        value = await self.gateway.call("inquiry_search_products", keyword=str(keyword))
        return value.get("products", [])

    async def get(self, sku):
        value = await self.gateway.call("inquiry_get_product", sku=str(sku))
        return value["product"]


class MCPQuoteCalculator:
    def __init__(self, gateway):
        self.gateway = gateway

    async def calculate(self, terms, products):
        value = await self.gateway.call(
            "inquiry_calculate_final_quote",
            terms_json=json.dumps(terms, ensure_ascii=False),
            products_json=json.dumps(products, ensure_ascii=False),
        )
        return value["quote"]
