"""Explicitly labelled demo fallbacks for interview-safe inquiry runs."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any

from providers.base import LLMProvider, LLMResponse


DOMAIN = re.compile(r"(?i)(?:https?://)?(?:www\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,})")


class OfflineDemoProvider(LLMProvider):
    """Signal deterministic planner/evaluator fallbacks when no model is available."""

    model = "local-demo-planner"

    async def chat(self, messages, tools=None, model=None) -> LLMResponse:
        # An invalid body intentionally activates the Agent's deterministic
        # planner/evaluator fallback instead of pretending a model responded.
        return LLMResponse(content="", finish_reason="error", usage={"model": self.model})


def _domain(query: str) -> str:
    match = DOMAIN.search(query or "")
    return match.group(1).casefold() if match else "demo-company.example"


def _public_excerpt(value: str) -> str:
    """Keep a short readable passage when the report model is unavailable."""
    raw = str(value or "")
    heading = re.search(r"(?:^|\n)\s*#{1,3}\s+(?:About|关于|公司简介)", raw, re.IGNORECASE)
    if heading:
        raw = raw[heading.start():]
    raw = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", raw)
    raw = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", raw)
    raw = re.sub(r"[#*`]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()[:500]


def demo_tool_result(tool: str, query: str) -> dict[str, Any]:
    """Build coherent local evidence. Every row is visibly marked as demo data."""
    company = re.sub(r"\s+", " ", str(query or "Demo Company")).strip()[:300]
    domain = _domain(company)
    website = f"https://{domain}/"
    common = {"demo_data": True, "data_source": "local_demo_fixture"}
    if tool == "official_website":
        text = (f"演示官网资料：{company}。页面展示企业业务、产品、公开政策和联系方式。"
                f"演示登记地址：{company}。演示电话：+1 555 010 2026。")
        raw = text.encode("utf-8")
        return {**common, "status": "ok", "candidate": {"url": website, "title": company},
                "search_results": [{"url": website, "title": company, "text": text}],
                "pages": [{"url": website, "title": company, "text": text, "status": "ok",
                           "sha256": hashlib.sha256(raw).hexdigest(), **common}]}
    if tool == "map_lookup":
        return {**common, "status": "ok", "results": [{
            "id": "demo-place-001", "displayName": {"text": company},
            "formattedAddress": company, "websiteUri": website,
            "nationalPhoneNumber": "+1 555 010 2026", "businessStatus": "OPERATIONAL", **common,
        }]}
    if tool == "customs_lookup":
        return {**common, "status": "ok", "data_level": "company_shipment", "results": [{
            "record_id": "demo-shipment-001", "company": company,
            "direction": "import", "product": "network equipment",
            "shipment_count": 6, "period": "last 12 months", **common,
        }]}
    if tool == "news_search":
        return {**common, "status": "ok", "results": [{
            "url": f"https://news.example/{domain}", "title": f"演示企业动态：{company}",
            "text": "用于面试演示的本地新闻资料，不能作为真实企业新闻。",
            "publisher": "演示新闻来源", "published_at": "2026-09-01T00:00:00Z",
            "evidence_quality": "page_fetched", "sha256": hashlib.sha256(company.encode()).hexdigest(),
            **common,
        }]}
    return {**common, "status": "unavailable", "reason": "demo_tool_not_supported", "results": []}


class DemoResilientResearchWeb:
    """Try the real adapter within a small budget, then use labelled local evidence."""

    def __init__(self, delegate, *, timeout_seconds=8, retries=1, prefer_fixtures=False,
                 allow_fixture_fallback=True):
        self.delegate = delegate
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.prefer_fixtures = prefer_fixtures
        self.allow_fixture_fallback = allow_fixture_fallback

    async def _call(self, tool: str, query: str):
        if self.prefer_fixtures and self.allow_fixture_fallback:
            return demo_tool_result(tool, query)
        last_reason = "external_service_unavailable"
        method = getattr(self.delegate, tool)
        for attempt in range(self.retries + 1):
            try:
                result = await asyncio.wait_for(method(query), timeout=self.timeout_seconds)
                if isinstance(result, dict) and result.get("status") == "ok":
                    return result
                if isinstance(result, dict):
                    last_reason = str(result.get("reason") or result.get("status") or last_reason)
            except Exception as exc:
                last_reason = str(getattr(exc, "code", type(exc).__name__))[:100]
            if attempt < self.retries:
                await asyncio.sleep(0.5 * (2 ** attempt))
        if not self.allow_fixture_fallback:
            return {"status": "unavailable", "reason": last_reason, "results": [], "pages": []}
        fallback = demo_tool_result(tool, query)
        fallback["fallback_reason"] = last_reason
        return fallback

    async def official_website(self, query):
        return await self._call("official_website", query)

    async def map_lookup(self, query):
        return await self._call("map_lookup", query)

    async def customs_lookup(self, query):
        return await self._call("customs_lookup", query)

    async def news_search(self, query):
        return await self._call("news_search", query)


class DemoReportProvider(LLMProvider):
    """Bound a real report call and produce an evidence-linked demo report on failure."""

    def __init__(self, delegate, *, timeout_seconds=45, prefer_fixture=False):
        self.delegate = delegate
        self.timeout_seconds = timeout_seconds
        self.prefer_fixture = prefer_fixture
        self.model = getattr(delegate, "model", "configured-report-provider")

    async def chat(self, messages: list[dict[str, Any]], tools=None, model=None) -> LLMResponse:
        if not self.prefer_fixture:
            try:
                response = await asyncio.wait_for(
                    self.delegate.chat(messages=messages, tools=tools, model=model),
                    timeout=self.timeout_seconds,
                )
                if response.finish_reason not in {"error", "length"} and response.content:
                    return response
            except Exception:
                pass
            fallback_model = getattr(self.delegate, "fallback_model", None)
            if fallback_model and fallback_model != (model or getattr(self.delegate, "model", None)):
                try:
                    response = await asyncio.wait_for(
                        self.delegate.chat(messages=messages, tools=tools, model=fallback_model),
                        timeout=min(30, self.timeout_seconds),
                    )
                    if response.finish_reason not in {"error", "length"} and response.content:
                        response.usage = {**(response.usage or {}), "fallback_used": True}
                        return response
                except Exception:
                    pass
        payload = self._payload(messages)
        report = self._report(payload)
        return LLMResponse(
            content=json.dumps(report, ensure_ascii=False), finish_reason="stop",
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                   "model": "local-demo-report", "fallback_used": True,
                   "demo_data": report["demo_data"]},
        )

    @staticmethod
    def _payload(messages):
        try:
            value = json.loads(str(messages[-1].get("content") or "{}"))
            return value if isinstance(value, dict) else {}
        except (ValueError, TypeError):
            return {}

    @staticmethod
    def _report(payload):
        identity = payload.get("company_identity") or {}
        evidence = payload.get("evidence") if isinstance(payload.get("evidence"), list) else []
        rfq = payload.get("rfq") if isinstance(payload.get("rfq"), dict) else {}
        messages = payload.get("customer_messages") if isinstance(payload.get("customer_messages"), list) else []
        message_ids = [str(row.get("message_id")) for row in messages
                       if isinstance(row, dict) and row.get("message_id")]

        def field_value(node):
            return node.get("value") if isinstance(node, dict) else node

        requested = []
        for item in (rfq.get("items") if isinstance(rfq.get("items"), list) else [])[:5]:
            if not isinstance(item, dict):
                continue
            product = str(field_value(item.get("product")) or "").strip()
            quantity = item.get("quantity")
            amount = field_value(quantity)
            unit = quantity.get("unit") if isinstance(quantity, dict) else ""
            if product:
                requested.append(" ".join(str(part) for part in (amount, unit, product) if part))
        destination = str(field_value(rfq.get("country")) or "").strip()
        request_summary = "、".join(requested) or "产品与数量尚未明确"
        if destination:
            request_summary += f"；目的地：{destination}"
        if message_ids:
            request_summary = f"客户邮件询问：{request_summary}。"
        else:
            request_summary = f"询盘记录：{request_summary}。"
        message_text = " ".join(str(row.get("text") or "") for row in messages if isinstance(row, dict)).casefold()
        simulated_inquiry = bool(re.search(
            r"workflow test|demonstration inquiry|流程测试|模拟询盘|不代表.{0,30}公司|not representing",
            message_text,
        ))
        external = [row for row in evidence if row.get("source_type") not in {
            "customer_message", "business_history"}]
        demo_data = any(bool(row.get("demo_data")) for row in external)
        by_tool: dict[str, list[dict]] = {}
        for row in external:
            by_tool.setdefault(str(row.get("source_type")), []).append(row)
        refs = lambda tool: [str(row.get("evidence_id")) for row in by_tool.get(tool, [])
                             if row.get("evidence_id")][:3]
        facts = []
        if message_ids:
            facts.append({"field_code": "other", "field": "客户邮件中的需求",
                          "value": request_summary, "status": "customer_provided",
                          "evidence_ids": message_ids[:5]})
        if identity.get("legal_name"):
            identity_refs = refs("official_website") + refs("map_lookup")
            facts.append({"field_code": "company_identity", "field": "公开网页对应的公司名称", "value": identity["legal_name"],
                          "status": "supported" if identity_refs else "unverified", "evidence_ids": identity_refs})
        site_rows = by_tool.get("official_website", [])
        for row in site_rows[:3]:
            if row.get("url") and row.get("evidence_id") and not row.get("demo_data"):
                facts.append({"field_code": "company_overview", "field": "检索到的企业网页",
                              "value": (f"官网页面《{row.get('title') or row['url']}》："
                                        f"{_public_excerpt(row.get('text'))}"),
                              "status": "supported", "evidence_ids": [row["evidence_id"]]})
        if by_tool.get("customs_lookup"):
            for row in by_tool["customs_lookup"][:2]:
                facts.append({"field_code": "customs_activity", "field": "海关货运记录",
                              "value": ("演示数据：" if row.get("demo_data") else "来源记录：")
                              + str(row.get("product") or row.get("description") or row.get("record_id") or "存在货运记录")[:250],
                              "status": "supported", "evidence_ids": [row["evidence_id"]]})
        credible_news = [row for row in by_tool.get("news_search", [])
                         if row.get("quality") in {"verified_page", "provider_extracted", "demo_fixture"}]
        if credible_news:
            for row in credible_news[:3]:
                official_news = bool(identity.get("official_domain") and
                                     identity["official_domain"] in str(row.get("url") or ""))
                facts.append({"field_code": "news_check", "field": "新闻核查",
                              "value": ("演示新闻：" if row.get("demo_data") else
                                        "官网动态：" if official_news else "公开报道：")
                              + str(row.get("title") or row.get("url") or "未取得标题")[:250],
                              "status": "supported", "evidence_ids": [row["evidence_id"]]})
        map_rows = by_tool.get("map_lookup", [])
        website = next((row.get("url") for row in site_rows if row.get("url")), "")
        address = next((row.get("formattedAddress") for row in map_rows if row.get("formattedAddress")), "")
        phone = next((row.get("nationalPhoneNumber") for row in map_rows if row.get("nationalPhoneNumber")), "")
        contact = lambda value, ids: [{"value": value, "evidence_ids": ids}] if value and ids else []
        public_summary = (f"找到 {len(site_rows)} 条公司网页和 {len(by_tool.get('news_search', []))} 条新闻资料，具体内容与链接见下方来源。"
                          if site_rows or by_tool.get("news_search") else "本次没有取得可引用的公开网页，外部调查结果不足。")
        return {
            "summary": (request_summary +
                        ("邮件明确是模拟询盘，不能当作该公司的真实采购需求。" if simulated_inquiry else
                         "发件人与所述企业的代表关系尚未核实。") +
                        public_summary +
                        ("本次部分调查使用本地演示数据，不能作为真实企业事实。" if demo_data else
                         "公开来源不完整的部分仍需人工核实。")),
            "facts": facts,
            "company_contacts": {
                "website": contact(website, refs("official_website")), "email": [],
                "phone": contact(phone, refs("map_lookup")), "address": contact(address, refs("map_lookup")),
            },
            "inquiry_contact": {"name": "", "role": "", "email": "", "whatsapp": "",
                                "relationship_status": "unverified"},
            "gaps": (["当前报告包含演示数据，正式使用前需用真实外部接口重新执行。"]
                     if demo_data else ["外部资料不完整；未取得来源的公司信息需要人工核实。"])
                    + (["邮件标明为模拟询盘，不得视为真实采购。"] if simulated_inquiry else []),
            "conflicts": [], "demo_data": demo_data,
        }
