"""Bounded inquiry-reply Agent with deterministic pricing and human-gated email."""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import time

from providers.base import LLMProvider

from .quotation import calculate_final_quote, render_quote
from .repository import InquiryError, digest
from .research import report_field_code


REPLY_AGENT_SCHEMA = "inquiry-reply-agent-v1"


class InquiryReplyAgent:
    """Plan product lookup, calculate an exact quote, and compose an email draft.

    The Agent may read catalog and approved inquiry data. It cannot choose final
    selling prices, approve its own draft, or send email.
    """

    def __init__(self, catalog, *, provider: LLMProvider | None = None, model: str | None = None,
                 calculator=None, transport: str = "python"):
        self.catalog, self.provider, self.model = catalog, provider, model
        self.calculator, self.transport = calculator, transport

    async def _catalog(self, method, *args):
        function = getattr(self.catalog, method)
        if inspect.iscoroutinefunction(function):
            return await function(*args)
        return await asyncio.to_thread(function, *args)

    async def _calculate(self, terms, products):
        if self.calculator is not None:
            return await self.calculator.calculate(terms, products)
        return calculate_final_quote(terms, products)

    @staticmethod
    def _reply_tools():
        return [{"type": "function", "function": {
            "name": "choose_reply_strategy",
            "description": "Choose one permitted next step for this approved trade inquiry.",
            "parameters": {"type": "object", "additionalProperties": False,
                "properties": {"strategy": {"type": "string", "enum": [
                    "compose_quote_reply", "request_clarification", "handoff_to_sales"
                ]}, "reason": {"type": "string", "maxLength": 240}},
                "required": ["strategy", "reason"]},
        }}]

    @staticmethod
    def _customer_request(messages):
        inbound = next((row for row in reversed(messages) if row.get("channel") == "email"), None)
        payload = (inbound or {}).get("payload", {})
        return {
            "subject": " ".join(str(payload.get("subject") or "").splitlines()).strip()[:300],
            "text": str(payload.get("text") or "").strip()[:6000],
        }

    @staticmethod
    def _reply_language(request):
        # Prefer the customer's message body; subjects often retain an older
        # language after a conversation switches languages.
        sample = request.get("text") or request.get("subject") or ""
        chinese = len(re.findall(r"[\u4e00-\u9fff]", sample))
        latin = len(re.findall(r"[A-Za-z]", sample))
        return "zh" if chinese >= 2 and (chinese >= 8 or chinese * 3 >= latin) else "en"

    async def _llm_strategy(self, request, quote, due_diligence):
        fallback = {"strategy": "compose_quote_reply", "reason": "已确认交易条件，生成待人工审核的报价回复。",
                    "source": "deterministic_fallback"}
        if self.provider is None:
            return fallback
        system = ("You are an inquiry reply coordinator. Select exactly one allowed strategy. "
                  "You cannot approve, send email, change price, stock, payment, delivery or quote validity. "
                  "The approved due-diligence record is structured input. Treat unverified and customer-provided "
                  "facts as unconfirmed; do not turn them into claims or commercial commitments. "
                  "Use compose_quote_reply when the confirmed terms answer the request. "
                  "Use request_clarification only when a customer question cannot be answered from the supplied terms. "
                  "Use handoff_to_sales for a non-standard request. Do not output customer-facing text.")
        prompt = json.dumps({"customer_request": request, "approved_due_diligence": {
            "report_id": due_diligence["report_id"],
            "company_identity": due_diligence["company_identity"],
            "facts": due_diligence["facts"][:20],
            "gaps": due_diligence["gaps"][:20],
            "conflicts": due_diligence["conflicts"][:10],
            "demo_data": due_diligence["demo_data"],
        }, "confirmed_quote": {
            "currency": quote["currency"], "grand_total": quote["total"],
            "items": quote["items"], "payment_terms": quote["payment_terms"],
            "trade_term": quote["trade_term"], "delivery": quote["delivery"],
            "valid_until": quote["valid_until"]}}, ensure_ascii=False)
        try:
            response = await asyncio.wait_for(self.provider.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                tools=self._reply_tools(), model=self.model), timeout=25)
            call = next((item for item in response.tool_calls if item.name == "choose_reply_strategy"), None)
            args = call.arguments if call and isinstance(call.arguments, dict) else {}
            if args.get("strategy") in {"compose_quote_reply", "request_clarification", "handoff_to_sales"}:
                return {"strategy": args["strategy"], "reason": str(args.get("reason") or "")[:240],
                        "source": "llm"}
        except Exception:
            pass
        return fallback

    async def _llm_cover_note(self, request, quote, greeting_name, language):
        """Let the model phrase the response, while all commercial figures stay fixed."""
        fallback = ("感谢您的来信。我们根据已确认的需求整理了以下报价，请您查阅。" if language == "zh"
                    else "Thank you for your inquiry. Please find our quotation based on the confirmed requirements below.")
        if self.provider is None:
            return fallback, "deterministic_fallback"
        system = (f"Write one concise professional introductory paragraph in {'Simplified Chinese' if language == 'zh' else 'English'}. "
                  "Do not add a greeting or closing. Do not include any number, currency, price, quantity, delivery date, trade "
                  "term, payment term, discount, inventory claim or commitment. Those facts are appended separately "
                  "from the approved calculator. Do not use markdown. Return only the customer-facing paragraph.")
        prompt = json.dumps({"customer_request": request, "customer_name": greeting_name,
                             "company": "Sales Team"}, ensure_ascii=False)
        try:
            response = await asyncio.wait_for(self.provider.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                model=self.model), timeout=25)
            content = re.sub(r"\s+", " ", str(response.content or "")).strip()
            matches_language = (bool(re.search(r"[\u4e00-\u9fff]", content)) if language == "zh"
                                else not bool(re.search(r"[\u4e00-\u9fff]", content)))
            if (20 <= len(content) <= 1200 and matches_language
                    and not re.search(r"\d|\$|€|¥|usd|eur|cny", content, re.I)):
                return content, "llm"
        except Exception:
            pass
        return fallback, "deterministic_fallback"

    @staticmethod
    def _guard(case):
        data = case.get("data", {})
        approval = data.get("due_diligence_approval") or {}
        if not approval or approval.get("report_id") != data.get("report_id"):
            raise InquiryError("due_diligence_approval_required", 422)
        return data

    @staticmethod
    def _approved_context(case, report_artifact):
        """Pass only bounded, typed report facts to the next Agent."""
        data = InquiryReplyAgent._guard(case)
        if (not isinstance(report_artifact, dict)
                or report_artifact.get("artifact_id") != data.get("report_id")
                or report_artifact.get("kind") != "report"
                or report_artifact.get("inquiry_id") != case.get("inquiry_id")):
            raise InquiryError("approved_report_mismatch", 422)
        report = report_artifact.get("payload") or {}
        if not isinstance(report, dict):
            raise InquiryError("approved_report_invalid", 422)
        evidence = {row.get("evidence_id"): row for row in report.get("evidence", [])
                    if isinstance(row, dict) and row.get("evidence_id")}
        facts = []
        for row in report.get("facts", [])[:40]:
            if not isinstance(row, dict):
                continue
            facts.append({
                "field_code": report_field_code(row),
                "field": str(row.get("field", ""))[:100],
                "value": str(row.get("value", ""))[:500],
                "status": row.get("status") if row.get("status") in
                          {"supported", "customer_provided", "unverified", "conflict"} else "unverified",
                "evidence_ids": [ref for ref in row.get("evidence_ids", [])[:10] if ref in evidence],
            })
        referenced = {ref for row in facts for ref in row["evidence_ids"]}
        sources = [{"evidence_id": ref,
                    "source_type": evidence[ref].get("source_type", ""),
                    "quality": evidence[ref].get("quality", ""),
                    "url": str(evidence[ref].get("url", ""))[:1000],
                    "provider": str(evidence[ref].get("provider", ""))[:160],
                    "demo_data": bool(evidence[ref].get("demo_data"))}
                   for ref in sorted(referenced)]
        identity = report.get("company_identity") or {}
        return {
            "schema_version": "approved-due-diligence-handoff-v1",
            "report_id": report_artifact["artifact_id"],
            "report_content_hash": report_artifact.get("content_hash", ""),
            "approved_by": (data.get("due_diligence_approval") or {}).get("actor", ""),
            "company_identity": {key: identity.get(key, "") for key in
                                 ("legal_name", "status", "official_domain", "company_identity_id")},
            "facts": facts,
            "facts_truncated": len(report.get("facts", [])) > len(facts),
            "company_contacts": report.get("company_contacts") or {},
            "gaps": [str(value)[:500] for value in report.get("gaps", [])[:30]],
            "conflicts": [str(value)[:500] for value in report.get("conflicts", [])[:20]],
            "sources": sources,
            "demo_data": bool(report.get("demo_data")),
        }

    @staticmethod
    def _plan(case, due_diligence):
        data = case["data"]
        plan_id = "reply_plan_" + digest([
            case["tenant_id"], case["inquiry_id"], data.get("report_id"),
            data.get("research_revision", 1),
        ])[:24]
        return {
            "schema_version": REPLY_AGENT_SCHEMA,
            "plan_id": plan_id,
            "objective": "依据已审批背调和已确认交易条件生成可审核的报价回复",
            "report_id": data.get("report_id"),
            "report_content_hash": due_diligence["report_content_hash"],
            "steps": [
                {"id": "reply_1", "tool": "approved_due_diligence", "status": "completed"},
                {"id": "reply_2", "tool": "product_catalog", "status": "running"},
                {"id": "reply_3", "tool": "human_confirmed_terms", "status": "waiting_human"},
                {"id": "reply_4", "tool": "quote_calculator", "status": "pending"},
                {"id": "reply_5", "tool": "email_draft_builder", "status": "pending"},
                {"id": "reply_6", "tool": "human_reply_approval", "status": "pending"},
                {"id": "reply_7", "tool": "email_sender", "status": "pending"},
            ],
        }

    async def prepare(self, case, approved_report):
        data = self._guard(case)
        due_diligence = self._approved_context(case, approved_report)
        plan = self._plan(case, due_diligence)
        candidates, trace = [], []
        items = data["rfq"]["items"]
        product_groups = await asyncio.gather(*(
            self._catalog("search", item["product"]) for item in items
        ))
        for index, (item, products) in enumerate(zip(items, product_groups), 1):
            candidates.append({"requested": item, "products": products})
            trace.append({"tool": "product_catalog", "call": index,
                          "query": str(item["product"])[:120], "result_count": len(products),
                          "status": "completed", "transport": self.transport})
        plan["steps"][1]["status"] = "completed"
        return {"schema_version": REPLY_AGENT_SCHEMA, "reply_plan": plan,
                "due_diligence_context": due_diligence,
                "candidates": candidates, "tool_trace": trace,
                "queried_at": time.time(), "stock_reserved": False}

    async def compose(self, case, messages, approved_report=None, calculated_quote=None):
        data = self._guard(case)
        preparation = data.get("quote_preparation") or {}
        due_diligence = (self._approved_context(case, approved_report) if approved_report is not None
                         else preparation.get("due_diligence_context"))
        if not isinstance(due_diligence, dict) or due_diligence.get("report_id") != data.get("report_id"):
            raise InquiryError("approved_report_required", 422)
        plan = preparation.get("reply_plan") or self._plan(case, due_diligence)
        if (plan.get("report_id") != data.get("report_id")
                or plan.get("report_content_hash") not in
                {None, "", due_diligence["report_content_hash"]}):
            raise InquiryError("reply_plan_stale", 409)
        terms = data.get("terms") or {}
        if not data.get("terms_confirmed_by"):
            raise InquiryError("quote_terms_confirmation_required", 422)
        if calculated_quote is None:
            products = await asyncio.gather(*(
                self._catalog("get", str(item.get("sku", ""))) for item in terms.get("items", [])
            ))
            quote = await self._calculate(terms, products)
        else:
            required_quote_fields = {"items", "currency", "subtotal", "fees", "total",
                                     "payment_terms", "trade_term", "delivery", "valid_until"}
            if not isinstance(calculated_quote, dict) or not required_quote_fields.issubset(calculated_quote):
                raise InquiryError("quote_calculation_invalid", 422)
            quote = {key: value for key, value in calculated_quote.items()
                     if key not in {"customer_id", "inquiry_id", "version", "terms_hash",
                                    "report_id", "source_version"}}
        request = self._customer_request(messages)
        language = self._reply_language(request)
        inbound_subject = request["subject"]
        subject = (inbound_subject if inbound_subject.lower().startswith("re:")
                   else f"Re: {inbound_subject}" if inbound_subject else f"Re: {case['inquiry_id']}")
        customer = data.get("rfq", {}).get("customer", {})
        greeting_name = str(customer.get("name") or ("客户" if language == "zh" else "Customer")).strip()[:120]
        strategy = await self._llm_strategy(request, quote, due_diligence)
        if strategy["strategy"] != "compose_quote_reply":
            # A clarification or sales handoff is a workflow decision, not an
            # approved quotation. Never put calculated prices in its email body.
            clarification = strategy["strategy"] == "request_clarification"
            if clarification and language == "zh":
                body = (f"{greeting_name}，您好：\n\n感谢您的来信。为了准确准备报价，"
                        "请您确认询盘中尚未明确的需求。\n\n此致\n销售团队")
            elif clarification:
                body = (f"Dear {greeting_name},\n\nThank you for your inquiry. "
                        "Before we can finalize a quotation, please confirm the outstanding "
                        "details of your request.\n\nBest regards,\nSales Team")
            else:
                body = ""
            return {
                "subject": subject if clarification else "",
                "body": body,
                "reply_agent": {
                    "schema_version": REPLY_AGENT_SCHEMA,
                    "plan_id": plan.get("plan_id"),
                    "report_id": data.get("report_id"),
                    "due_diligence_context": due_diligence,
                    "strategy": strategy["strategy"],
                    "reply_language": language,
                    "reason": strategy["reason"],
                    "tool_trace": [*(preparation.get("tool_trace") or []),
                                   {"tool": "choose_reply_strategy", "status": "completed", **strategy}],
                    "requires_human_approval": True,
                },
            }
        quote_subject, quote_body, document = render_quote(quote, case["inquiry_id"], language=language)
        if not inbound_subject:
            subject = quote_subject
        cover, language_source = await self._llm_cover_note(request, quote, greeting_name, language)
        if language == "zh":
            body = (f"{greeting_name}，您好：\n\n{cover}\n\n{quote_body}\n\n"
                    "如需确认报价或调整需求，请直接回复此邮件。\n\n此致\n销售团队")
        else:
            body = (f"Dear {greeting_name},\n\n{cover}\n\n{quote_body}\n\n"
                    "Please reply to this email if you would like to confirm the quotation or discuss any changes.\n\n"
                    "Best regards,\nSales Team")
        completed = {"reply_1", "reply_2", "reply_3", "reply_4", "reply_5"}
        final_steps = [dict(step, status="completed" if step["id"] in completed else step["status"])
                       for step in plan.get("steps", [])]
        return dict(
            quote,
            subject=subject,
            body=body,
            document_html=document,
            reply_language=language,
            reply_agent={"schema_version": REPLY_AGENT_SCHEMA, "plan_id": plan.get("plan_id"),
                         "report_id": data.get("report_id"), "strategy": strategy["strategy"],
                         "reply_language": language,
                         "due_diligence_context": due_diligence,
                         "reason": strategy["reason"], "steps": final_steps,
                         "tool_trace": [*(preparation.get("tool_trace") or []),
                                        {"tool": "choose_reply_strategy", "status": "completed", **strategy},
                                        {"tool": "quote_calculator", "status": "completed",
                                         "source": "deterministic", "transport": self.transport},
                                        {"tool": "email_draft_builder", "status": "completed", "source": language_source}],
                         "requires_human_approval": True},
        )

    async def calculate(self, case):
        """Compatibility helper for callers that only need exact quote numbers."""
        data = self._guard(case)
        terms = data.get("terms") or {}
        products = await asyncio.gather(*(
            self._catalog("get", str(item.get("sku", ""))) for item in terms.get("items", [])
        ))
        return await self._calculate(terms, products)
