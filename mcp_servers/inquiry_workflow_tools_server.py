"""MCP tools for the auditable inquiry workflow.

The server only exposes bounded read tools and the deterministic quote
calculator. Workflow state, approvals and email sending stay in the host
application so an MCP tool can never approve or send its own output.
"""
from __future__ import annotations

from dataclasses import asdict
import json

from mcp.server.fastmcp import FastMCP

from agent.inquiry.config import InquirySettings
from agent.inquiry.quotation import calculate_final_quote
from agent.inquiry.research import ResearchWeb


mcp = FastMCP("inquiry-workflow-tools")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _web():
    settings = InquirySettings.from_env()
    # The host decides per inquiry whether demo fixtures are allowed. Keeping
    # this MCP adapter real prevents live mailbox cases from silently receiving
    # synthetic evidence when DEMO_MODE is enabled for the whole process.
    return ResearchWeb(
        settings.search_key,
        search_provider=settings.search_provider,
        map_key=settings.map_api_key,
        customs_key=settings.customs_api_key,
        customs_base_url=settings.customs_base_url,
        customs_provider=settings.customs_provider,
        customs_records_per_direction=settings.customs_records_per_direction,
        news_key=settings.news_api_key,
        crawl_max_pages=settings.crawl_max_pages,
        crawl_max_depth=settings.crawl_max_depth,
    )


@mcp.tool()
async def research_official_website(query: str) -> str:
    """Search and crawl the target company's public official website."""
    return _json(await _web().official_website(query))


@mcp.tool()
async def research_map_lookup(query: str) -> str:
    """Look up public company place and address information."""
    return _json(await _web().map_lookup(query))


@mcp.tool()
async def research_customs_lookup(query: str) -> str:
    """Look up company-level customs data using the configured provider."""
    return _json(await _web().customs_lookup(query))


@mcp.tool()
async def research_news_search(query: str) -> str:
    """Search recent public company news and risk signals."""
    return _json(await _web().news_search(query))


@mcp.tool()
def inquiry_search_products(keyword: str) -> str:
    """Search the internal product catalog for quote candidates."""
    from agent.business.database import list_products
    rows = [asdict(p) for p in list_products(keyword=str(keyword)[:120], limit=10)]
    settings = InquirySettings.from_env()
    if not rows and settings.demo_mode and "AX55" in str(keyword).upper():
        rows = [{"sku": "DEMO-AX55-EU", "name_en": "Archer AX55 EU",
                 "name_cn": "Archer AX55 双频 Wi-Fi 6 路由器", "unit": "pcs",
                 "inventory": 200, "moq": 10, "price_usd": 79,
                 "active": True, "demo_data": True}]
    return _json({"status": "ok", "products": rows})


@mcp.tool()
def inquiry_get_product(sku: str) -> str:
    """Get one active product required by an approved quote."""
    from agent.business.database import get_product_by_sku
    product = get_product_by_sku(str(sku)[:120])
    settings = InquirySettings.from_env()
    if not product and settings.demo_mode and sku == "DEMO-AX55-EU":
        value = {"sku": sku, "name_en": "Archer AX55 EU",
                 "name_cn": "Archer AX55 双频 Wi-Fi 6 路由器", "unit": "pcs",
                 "inventory": 200, "moq": 10, "price_usd": 79,
                 "active": True, "demo_data": True}
        return _json({"status": "ok", "product": value})
    if not product or not product.active:
        return _json({"status": "error", "error": "quote_product_unavailable"})
    return _json({"status": "ok", "product": asdict(product)})


@mcp.tool()
def inquiry_calculate_final_quote(terms_json: str, products_json: str) -> str:
    """Calculate exact quote totals from human-confirmed terms; no LLM math."""
    try:
        terms, products = json.loads(terms_json), json.loads(products_json)
        return _json({"status": "ok", "quote": calculate_final_quote(terms, products)})
    except Exception as exc:
        return _json({"status": "error", "error": getattr(exc, "code", type(exc).__name__)})


if __name__ == "__main__":
    mcp.run(transport="stdio")
