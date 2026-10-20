"""Outbox runner: restartable reads/computation and human-gated external sends."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
import json
import logging
import re
import time

from .config import InquirySettings
from .reply import InquiryReplyAgent
from .repository import InquiryError, InquiryRepository, digest, encode, new_id
from .research import DueDiligenceAgent, ResearchWeb
from .service import (InquiryService, normalized_rfq, rfq_gaps, quote_entry_gaps,
                      clarification_body, follow_up_after_send, EMAIL)
from .storage import (DatabaseResearchMemory, DurableResearchMemory, LocalDemoEmailArchive,
                      RedisResearchMemory, S3EmailArchive)


DEMO_AX55_PRODUCT = {
    "sku": "DEMO-AX55-EU", "name_en": "Archer AX55 EU",
    "name_cn": "Archer AX55 双频 Wi-Fi 6 路由器", "unit": "pcs",
    "inventory": 200, "moq": 10, "price_usd": 79,
    "active": True, "demo_data": True,
}

_COMPANY_LABEL = re.compile(
    r"(?im)^\s*(?:客户公司名称|公司名称|公司名|所属公司|公司|Company(?:\s+Name)?)\s*[:：]\s*([^\r\n]{2,120})"
)
_COMPANY_DECLARATION = re.compile(
    r"(?:我司|我们公司|我们是|来自)\s*([\u4e00-\u9fffA-Za-z0-9·（）()\-]{2,100}?(?:股份有限公司|有限责任公司|有限公司))"
)
_PUBLIC_MAIL_DOMAINS = {"qq.com", "gmail.com", "163.com", "126.com", "outlook.com",
                        "hotmail.com", "yahoo.com", "foxmail.com", "icloud.com"}


def _company_from_message(body: str) -> str:
    for pattern in (_COMPANY_LABEL, _COMPANY_DECLARATION):
        match = pattern.search(body)
        if match:
            name = match.group(1).strip(" \t，,。.;；")
            if name:
                return name
    return ""


class Catalog:
    def search(self, keyword):
        settings = InquirySettings.from_env()
        demo_match = settings.demo_mode and "AX55" in str(keyword).upper()
        if settings.demo_mode and settings.demo_prefer_fixtures:
            return [dict(DEMO_AX55_PRODUCT)] if demo_match else []
        try:
            from agent.business.database import list_products
            rows = [asdict(p) for p in list_products(keyword=str(keyword)[:120], limit=10)]
        except Exception:
            if not settings.demo_mode:
                raise
            rows = []
        if not rows and demo_match:
            rows = [dict(DEMO_AX55_PRODUCT)]
        return rows

    def get(self, sku):
        settings = InquirySettings.from_env()
        if settings.demo_mode and settings.demo_prefer_fixtures:
            if sku == DEMO_AX55_PRODUCT["sku"]:
                return dict(DEMO_AX55_PRODUCT)
            raise InquiryError("quote_product_unavailable", 422)
        try:
            from agent.business.database import get_product_by_sku
            product = get_product_by_sku(sku)
        except Exception:
            if not settings.demo_mode:
                raise
            product = None
        if not product and settings.demo_mode and sku == DEMO_AX55_PRODUCT["sku"]:
            return dict(DEMO_AX55_PRODUCT)
        if not product or not product.active:
            raise InquiryError("quote_product_unavailable", 422)
        return asdict(product)


QuoteAgent = InquiryReplyAgent  # Backward-compatible import for existing integrations.


class DemoEmailSender:
    """Local email tool used only when DEMO_MODE is explicitly enabled."""

    def build_message(self, delivery, account):
        message = EmailMessage()
        message["From"] = "demo-sales@nanoclaw.local"
        message["To"] = delivery["recipient"]
        message["Subject"] = delivery["subject_snapshot"]
        message["Message-ID"] = delivery["smtp_message_id"]
        if delivery.get("in_reply_to"):
            message["In-Reply-To"] = delivery["in_reply_to"]
        message.set_content(delivery["body_snapshot"])
        if delivery.get("quotation_html"):
            message.add_attachment(delivery["quotation_html"].encode("utf-8"),
                                   maintype="text", subtype="html", filename="quotation.html")
        return message

    def send(self, delivery, account):
        fingerprint = [delivery.get("recipient"), delivery.get("subject_snapshot"),
                       delivery.get("smtp_message_id")]
        return f"<demo-accepted-{digest(fingerprint)[:20]}@nanoclaw.local>"


class ApprovedMailer:
    def __init__(self, settings, archive, accounts=None, sender=None):
        self.settings, self.archive = settings, archive
        self.accounts, self.sender = accounts, sender

    def prepare(self, case, artifact):
        quote = artifact["payload"]
        if artifact["kind"] == "clarification":
            if case["data"].get("clarification_id") != artifact["artifact_id"] or not quote.get("approved_by"):
                raise InquiryError("clarification_approval_required")
        else:
            approval = case["data"].get("quote_approval", {})
            if approval.get("quote_id") != artifact["artifact_id"] or approval.get("hash") != artifact["content_hash"]:
                raise InquiryError("quote_approval_stale")
            if datetime.fromisoformat(quote["valid_until"].replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                raise InquiryError("quote_expired")
        if case["data"].get("demo_fixture") or not self.settings.smtp_enabled:
            if not self.settings.demo_mode:
                raise InquiryError("inquiry_smtp_not_enabled", 503)
            sender = DemoEmailSender()
            delivery = {"recipient": quote["recipient"], "subject_snapshot": quote["subject"],
                        "body_snapshot": quote["body"], "smtp_message_id": quote["smtp_message_id"],
                        "in_reply_to": quote.get("in_reply_to", ""),
                        "quotation_html": quote.get("document_html", ""),
                        "auto_submitted": artifact["kind"] == "clarification",
                        "demo_delivery": True}
            raw = sender.build_message(delivery, {}).as_bytes(policy=policy.SMTP)
            delivery["original_bytes"] = raw
            # The worker supplies the same local archive for incoming and
            # outgoing demo mail, so a single case always has one inspectable
            # mailbox location.
            archive = self.archive.put(case["tenant_id"], quote["account_id"], raw)
            return sender, delivery, {"demo": True}, archive
        from agent.business.email_account_service import create_default_email_account_service
        from agent.business.email_secret_store import create_default_email_secret_store
        from channels.email.smtp_sender import SmtpSslSender
        accounts = self.accounts or create_default_email_account_service().repository
        account = accounts.get(artifact["payload"]["account_id"])
        if not account or not account["outbound_enabled"] or account["status"] not in {"healthy", "degraded"}:
            raise InquiryError("email_account_not_sendable", 422)
        recipient = quote["recipient"].casefold()
        original_sender = str(case["data"].get("recipient") or "").casefold()
        allowed_recipients = {x.casefold() for x in account.get("allowed_recipients", [])}
        if artifact["kind"] == "clarification":
            original_account = str(case["data"].get("account_id") or "")
            if (not original_sender or recipient != original_sender
                    or str(quote.get("account_id") or "") != original_account):
                raise InquiryError("clarification_recipient_mismatch", 422)
        elif recipient not in allowed_recipients:
            raise InquiryError("email_recipient_not_allowed", 422)
        sender = self.sender or SmtpSslSender(create_default_email_secret_store())
        delivery = {"recipient": quote["recipient"], "subject_snapshot": quote["subject"],
                    "body_snapshot": quote["body"], "smtp_message_id": quote["smtp_message_id"],
                    "in_reply_to": quote.get("in_reply_to", ""), "quotation_html": quote.get("document_html", ""),
                    "auto_submitted": artifact["kind"] == "clarification"}
        raw = sender.build_message(delivery, account).as_bytes(policy=policy.SMTP)
        archive = self.archive.put(case["tenant_id"], quote["account_id"], raw)
        delivery["original_bytes"] = raw
        return sender, delivery, account, archive


class InquiryWorker:
    def __init__(self, service, *, settings=None, extractor=None, research=None, quotation=None, mailer=None,
                 mcp_manager=None):
        self.service, self.repo = service, service.repo
        self.settings = settings or InquirySettings.from_env()
        self.extractor, self.research = extractor, research
        self.mcp_gateway = None
        if mcp_manager is not None:
            from .mcp_tools import InquiryMCPGateway
            candidate = InquiryMCPGateway(mcp_manager)
            if candidate.available:
                self.mcp_gateway = candidate
        self.quotation = quotation or self._build_reply_agent()
        archive = (LocalDemoEmailArchive() if self.settings.demo_mode else S3EmailArchive(self.settings))
        self.mailer = mailer or ApprovedMailer(self.settings, archive)

    def _build_reply_agent(self):
        """Use the configured model only for the bounded reply coordinator.

        The calculator, attachment and approval gate do not depend on the model,
        so a missing or unavailable model still leaves a reviewable draft.
        """
        provider = None
        if not (self.settings.demo_mode and self.settings.demo_prefer_fixtures):
            try:
                from config import load_config
                from providers.openai_compat import OpenAICompatProvider
                config = load_config()
                if config.api_key:
                    provider = OpenAICompatProvider(config.api_key, config.base_url, config.model)
            except Exception:
                logging.getLogger(__name__).warning("Inquiry reply model unavailable; using deterministic cover note")
        if self.settings.demo_mode and self.settings.demo_prefer_fixtures:
            return QuoteAgent(Catalog(), provider=None, transport="local_demo")
        if self.mcp_gateway is not None:
            from .mcp_tools import MCPCatalog, MCPQuoteCalculator
            return QuoteAgent(MCPCatalog(self.mcp_gateway), provider=provider,
                              calculator=MCPQuoteCalculator(self.mcp_gateway), transport="mcp")
        return QuoteAgent(Catalog(), provider=provider)

    async def execute(self, job, case):
        if job["kind"] == "extract":
            from agent.business.rfq_extractor import (extract_rfq_fields,
                deterministic_extract_rfq_fields)
            sources = self.service.messages(case["tenant_id"], case["inquiry_id"])
            excluded = set(case["data"].get("excluded_message_ids", []))
            sources = [x for x in sources if x["message_id"] not in excluded]
            text = "\n\n".join(f"Message {x['message_id']} from {x['payload']['sender']}:\n{x['payload']['text']}" for x in sources)
            latest = sources[-1]["payload"]
            extractor = self.extractor or extract_rfq_fields
            context = {"subject": latest.get("subject", ""),
                       "from_address": latest["sender"], "source": latest["channel"]}
            try:
                raw = await asyncio.wait_for(extractor(text[-60000:], context),
                                             timeout=30 if self.settings.demo_mode else 90)
            except Exception:
                if self.extractor is not None:
                    raise
                raw = deterministic_extract_rfq_fields(text[-60000:], context)
            company = _company_from_message(str(latest.get("text") or ""))
            if company and not raw.get("customer", {}).get("company", {}).get("value"):
                raw["customer"]["company"] = {"value": company, "status": "extracted", "evidence": company}
                raw["missing_fields"] = [item for item in raw.get("missing_fields", [])
                                         if item != "customer.company"]
            language = InquiryReplyAgent._reply_language(InquiryReplyAgent._customer_request(sources))
            return {"rfq": normalized_rfq(raw), "source_message_ids": [x["message_id"] for x in sources],
                    "reply_language": language, "source_channel": latest["channel"]}
        if job["kind"] == "research":
            research_agent = self.research
            if research_agent is None:
                from config import load_config
                from providers.openai_compat import OpenAICompatProvider
                from .report_provider import StructuredReportProvider
                config = load_config()
                if not config.api_key and not self.settings.demo_mode:
                    raise InquiryError("research_model_not_configured", 503)
                fixed_demo = (self.settings.demo_mode and self.settings.demo_prefer_fixtures
                              and bool(case["data"].get("demo_fixture")))
                if self.settings.demo_mode and (fixed_demo or not config.api_key):
                    from .demo import DemoReportProvider, OfflineDemoProvider
                    planner = OfflineDemoProvider()
                    report_provider = DemoReportProvider(
                        planner, timeout_seconds=self.settings.demo_report_timeout_seconds,
                        prefer_fixture=fixed_demo,
                    )
                else:
                    planner = OpenAICompatProvider(api_key=config.api_key, base_url=config.base_url, model=config.model)
                    report_uses_deepseek = bool(self.settings.report_api_key)
                    report_provider = StructuredReportProvider(
                        api_key=self.settings.report_api_key or config.api_key,
                        base_url=self.settings.report_base_url if report_uses_deepseek else config.base_url,
                        model=self.settings.report_model if report_uses_deepseek else config.model,
                        max_tokens=self.settings.report_max_tokens,
                        timeout_seconds=(self.settings.demo_report_timeout_seconds
                                         if self.settings.demo_mode else self.settings.report_timeout_seconds),
                        max_retries=(0 if self.settings.demo_mode else self.settings.report_max_retries),
                        reasoning_effort=("none" if self.settings.demo_mode and not fixed_demo else
                                          self.settings.report_reasoning_effort) if report_uses_deepseek else None,
                        fallback_model=self.settings.report_fallback_model if report_uses_deepseek else None,
                        fallback_max_retries=self.settings.report_fallback_max_retries,
                    )
                if self.mcp_gateway is not None:
                    from .mcp_tools import MCPResearchWeb
                    web = MCPResearchWeb(self.mcp_gateway)
                else:
                    web = ResearchWeb(
                        self.settings.search_key,
                        search_provider=self.settings.search_provider,
                        map_key=self.settings.map_api_key,
                        customs_key=self.settings.customs_api_key,
                        customs_base_url=self.settings.customs_base_url,
                        customs_provider=self.settings.customs_provider,
                        customs_records_per_direction=self.settings.customs_records_per_direction,
                        news_key=self.settings.news_api_key,
                        crawl_max_pages=self.settings.crawl_max_pages,
                        crawl_max_depth=self.settings.crawl_max_depth,
                    )
                if self.settings.demo_mode:
                    from .demo import DemoReportProvider, DemoResilientResearchWeb
                    web = DemoResilientResearchWeb(
                        web,
                        timeout_seconds=(self.settings.demo_external_timeout_seconds
                                         if fixed_demo else max(30, self.settings.demo_external_timeout_seconds)),
                        retries=self.settings.demo_external_retries if fixed_demo else 0,
                        prefer_fixtures=self.settings.demo_prefer_fixtures,
                        allow_fixture_fallback=fixed_demo,
                    )
                    if not isinstance(report_provider, DemoReportProvider):
                        report_provider = DemoReportProvider(
                            report_provider,
                            timeout_seconds=self.settings.demo_report_timeout_seconds,
                            prefer_fixture=fixed_demo,
                        )
                research_agent = DueDiligenceAgent(
                    planner,
                    DurableResearchMemory(
                        DatabaseResearchMemory(self.repo),
                        RedisResearchMemory(self.settings),
                    ),
                    web, self.settings.research_steps,
                    report_provider=report_provider,
                    report_repair_attempts=self.settings.report_repair_attempts,
                )
            with self.repo.tx() as tx:
                history = tx.all("SELECT * FROM inquiry_artifact WHERE tenant_id=? AND customer_id=? AND kind IN ('report','acceptance') ORDER BY created_at DESC LIMIT 10",
                                 (case["tenant_id"], case["customer_id"]))
            return await research_agent.run(case, self.service.messages(case["tenant_id"], case["inquiry_id"]), history)
        if job["kind"] == "prepare_quote":
            with self.repo.tx() as tx:
                approved_report = self.repo.artifact(case["tenant_id"], case["data"]["report_id"], tx)
            if approved_report["inquiry_id"] != case["inquiry_id"] or approved_report["kind"] != "report":
                raise InquiryError("approved_report_mismatch", 422)
            return await self.quotation.prepare(case, approved_report)
        if job["kind"] == "calculate":
            quote = await self.quotation.calculate(case)
            return dict(quote,
                        terms_hash=digest(case["data"].get("terms") or {}),
                        report_id=case["data"].get("report_id"),
                        source_version=case["version"])
        if job["kind"] == "compose_reply":
            messages = self.service.messages(case["tenant_id"], case["inquiry_id"])
            with self.repo.tx() as tx:
                approved_report = self.repo.artifact(case["tenant_id"], case["data"]["report_id"], tx)
                calculation = self.repo.artifact(
                    case["tenant_id"], case["data"].get("quote_calculation_id", ""), tx)
            if approved_report["inquiry_id"] != case["inquiry_id"] or approved_report["kind"] != "report":
                raise InquiryError("approved_report_mismatch", 422)
            if (calculation["inquiry_id"] != case["inquiry_id"]
                    or calculation["kind"] != "quote_calculation"
                    or calculation["payload"].get("terms_hash") != digest(case["data"].get("terms") or {})
                    or calculation["payload"].get("report_id") != case["data"].get("report_id")):
                raise InquiryError("quote_calculation_stale", 409)
            quote = await self.quotation.compose(
                case, messages, approved_report, calculated_quote=calculation["payload"])
            inbound = next((x for x in reversed(messages) if x["channel"] == "email"), None)
            return dict(quote,
                        account_id=case["data"]["terms"].get("outbound_account_id") or case["data"]["account_id"],
                        smtp_message_id=f"<{new_id('quote')}@nanoclaw.local>",
                        in_reply_to=inbound["external_id"] if inbound else "",
                        terms_confirmed_by=case["data"]["terms_confirmed_by"],
                        report_id=case["data"]["report_id"], source_version=case["version"])
        raise InquiryError("unknown_inquiry_job")

    async def run_once(self):
        job = self.repo.claim(self.settings.tenant_id)
        if not job:
            return None
        case = self.repo.get(job["tenant_id"], job["inquiry_id"])
        if job["kind"] in {"send", "clarify"}:
            return await self._send(job, case)
        try:
            research_revision_matches = (job["kind"] != "research"
                                         or job.get("research_revision") is None
                                         or int(job["research_revision"]) == int(case["data"].get("research_revision", 1)))
            parallel_research = (job["kind"] == "research"
                                 and research_revision_matches
                                 and case["data"].get("research_status") == "queued"
                                 and case["state"] in {"waiting_customer_info", "researching", "clarify_queued"})
            if not research_revision_matches or (case["version"] != job["expected_version"] and not parallel_research):
                with self.repo.tx() as tx:
                    self.repo.finish(tx, job, "superseded")
                return {"status": "superseded"}
            result = await self.execute(job, case)
            with self.repo.tx() as tx:
                current = self.repo.get(job["tenant_id"], job["inquiry_id"], tx)
                merge_research = (job["kind"] == "research"
                                  and (job.get("research_revision") is None
                                       or int(job["research_revision"]) == int(current["data"].get("research_revision", 1)))
                                  and current["data"].get("research_status") == "queued"
                                  and current["data"].get("research_revision") == case["data"].get("research_revision")
                                  and current["state"] in {"waiting_customer_info", "researching", "clarify_queued"})
                if current["version"] != case["version"] and not merge_research:
                    self.repo.finish(tx, job, "superseded")
                    return {"status": "superseded"}
                previous = current["version"]
                current["version"] += 1
                data = current["data"]
                if job["kind"] == "extract":
                    data.update(result)
                    data["gaps"] = rfq_gaps(result["rfq"])
                    company = str(result["rfq"].get("customer", {}).get("company") or "").strip()
                    automatic_email_flow = (self.settings.auto_clarify
                                            and result.get("source_channel") == "email")
                    # A company identity is needed for the next automated step.
                    # In automatic email mode, ask the customer for it instead of
                    # parking the RFQ in a staff review queue.
                    if automatic_email_flow and not company:
                        data["gaps"] = [gap for gap in data["gaps"]
                                        if gap != "customer.name_or_company"]
                        if "customer.company" not in data["gaps"]:
                            data["gaps"].append("customer.company")
                    data["clarification_questions"] = ["请补充：" + field for field in data["gaps"]]
                    data["reply_language"] = result.get("reply_language", "en")
                    data["clarification_draft"] = clarification_body(data["gaps"], data["reply_language"]) if data["gaps"] else ""
                    data["message_ids"] = result["source_message_ids"]
                    data["pending_message_ids"] = []
                    company_node = (result["rfq"].get("raw_extraction", {})
                                    .get("customer", {}).get("company", {}))
                    sender_email = str(result["rfq"].get("customer", {}).get("email") or "")
                    sender_domain = sender_email.rsplit("@", 1)[-1].lower()
                    corporate_sender = "@" in sender_email and sender_domain not in _PUBLIC_MAIL_DOMAINS
                    company_from_email = bool(company and isinstance(company_node, dict)
                                              and company_node.get("status") == "extracted")
                    identity_ready = company_from_email or corporate_sender or (
                        automatic_email_flow and bool(company))
                    current["state"] = ("waiting_customer_info" if data["gaps"] else
                                        "researching" if identity_ready else "waiting_rfq_review")
                    if identity_ready:
                        data["customer_type"] = "company"
                        data["research_status"] = "queued"
                        self.repo.enqueue(tx, current, "research")
                    if data["gaps"] and self.settings.auto_clarify and EMAIL.fullmatch(data.get("recipient", "")) and data.get("auto_clarify_count", 0) < 3:
                        data["clarification_id"] = self.repo.save_artifact(tx, current, "clarification", {
                            "recipient": data["recipient"], "account_id": data["account_id"],
                            "subject": ("请补充询盘信息 / " if data["reply_language"] == "zh"
                                        else "Additional information required / ") + current["inquiry_id"],
                            "body": data["clarification_draft"], "smtp_message_id": f"<{new_id('clarification')}@nanoclaw.local>",
                            "approved_by": "configured-auto-clarification-policy", "approved_at": time.time()})
                        data["auto_clarify_count"] = data.get("auto_clarify_count", 0) + 1
                        data["dispatch_kind"] = "clarify"
                        current["state"] = "clarify_queued"
                        self.repo.enqueue(tx, current, "clarify")
                elif job["kind"] == "research":
                    data["research_plan_id"] = self.repo.save_artifact(tx, current, "research_plan", {
                        "schema_version": result.get("schema_version"),
                        "plan": result.get("research_plan", {}),
                        "original_message_ids": [row.get("message_id") for row in result.get("original_messages", [])],
                    })
                    data["research_execution_id"] = self.repo.save_artifact(tx, current, "research_execution", {
                        "schema_version": result.get("schema_version"),
                        "execution_log": result.get("execution_log", []),
                        "evaluation": result.get("evaluation", {}),
                    })
                    data["report_id"] = self.repo.save_artifact(tx, current, "report", result)
                    identity = result.get("company_identity", {})
                    data["company_identity"] = identity
                    data["identity_gaps"] = identity.get("gaps", [])
                    rejected_identity = identity.get("status") == "rejected"
                    data["research_status"] = "identity_rejected" if rejected_identity else "complete"
                    data["research_completed_revision"] = data.get("research_revision")
                    data["quote_entry_gaps"] = quote_entry_gaps(data.get("rfq") or {})
                    if current["state"] in {"waiting_customer_info", "researching"}:
                        current["state"] = ("waiting_customer_info" if rejected_identity or data["quote_entry_gaps"]
                                            else "waiting_due_diligence_review")
                elif job["kind"] == "prepare_quote":
                    data["quote_preparation"] = result
                    data["reply_plan_id"] = self.repo.save_artifact(
                        tx, current, "reply_plan", result.get("reply_plan", {}))
                    current["state"] = "waiting_negotiation"
                elif job["kind"] == "calculate":
                    data["quote_calculation_id"] = self.repo.save_artifact(
                        tx, current, "quote_calculation", result)
                    current["state"] = "composing_reply"
                    self.repo.enqueue(tx, current, "compose_reply")
                elif job["kind"] == "compose_reply":
                    strategy = (result.get("reply_agent") or {}).get("strategy", "compose_quote_reply")
                    if strategy == "compose_quote_reply":
                        data["quote_id"] = self.repo.save_artifact(tx, current, "quote", result)
                        data["reply_draft_id"] = data["quote_id"]
                        current["state"] = "waiting_quote_approval"
                    elif strategy in {"request_clarification", "handoff_to_sales"}:
                        data["reply_decision_id"] = self.repo.save_artifact(tx, current, "reply_decision", result)
                        data.pop("quote_id", None)
                        data.pop("reply_draft_id", None)
                        if strategy == "request_clarification":
                            data["clarification_draft"] = result["body"]
                            data["clarification_reason"] = result["reply_agent"].get("reason", "")
                            current["state"] = "waiting_customer_info"
                        else:
                            data["sales_handoff_reason"] = result["reply_agent"].get("reason", "")
                            current["state"] = "waiting_negotiation"
                    else:
                        raise InquiryError("reply_strategy_invalid", 422)
                if data.get("failed_job") == job["kind"]:
                    data.pop("failed_job", None)
                    data.pop("error_code", None)
                self.repo.write_case(tx, current, previous)
                self.repo.event(tx, current, "worker", job["kind"] + "_completed")
                self.repo.finish(tx, job)
                return current
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._fail(job, exc)

    def _fail(self, job, exc, unknown=False):
        code = str(getattr(exc, "code", type(exc).__name__))[:100]
        with self.repo.tx() as tx:
            current = self.repo.get(job["tenant_id"], job["inquiry_id"], tx)
            research_revision_matches = (job["kind"] != "research"
                                         or job.get("research_revision") is None
                                         or int(job["research_revision"]) == int(current["data"].get("research_revision", 1)))
            parallel_research = (job["kind"] == "research"
                                 and research_revision_matches
                                 and current["data"].get("research_status") == "queued"
                                 and current["state"] in {"waiting_customer_info", "researching", "clarify_queued"})
            if current["version"] == job["expected_version"] or unknown or parallel_research:
                previous = current["version"]
                current["version"] += 1
                if parallel_research and current["data"].get("gaps"):
                    current["data"]["research_status"] = "failed"
                    current["data"]["research_error_code"] = code
                else:
                    current["state"] = "delivery_unknown" if unknown else "retry_pending"
                current["data"].update(error_code=code, failed_job=job["kind"])
                self.repo.write_case(tx, current, previous)
                self.repo.event(tx, current, "worker", "job_failed", {"error_code": code})
            if job["kind"] == "research" and research_revision_matches:
                self.repo.mark_research_failed(tx, current, code)
            self.repo.finish(tx, job, "unknown" if unknown else "failed", code)
        return {"status": "delivery_unknown" if unknown else "failed", "error_code": code}

    async def _send(self, job, case):
        # Reconcile an interrupted dispatch even when a new message changed the version.
        if job["recovered"] and case["data"].get("dispatch_started"):
            return self._fail(job, InquiryError("smtp_outcome_unknown"), unknown=True)
        clarification = job["kind"] == "clarify"
        expected_state = "clarify_queued" if clarification else "send_queued"
        if case["state"] != expected_state or (not clarification and case["version"] != job["expected_version"]):
            with self.repo.tx() as tx:
                self.repo.finish(tx, job, "superseded")
            return {"status": "superseded"}
        started = False
        try:
            with self.repo.tx() as tx:
                quote = self.repo.artifact(case["tenant_id"], case["data"]["clarification_id" if clarification else "quote_id"], tx)
            sender, delivery, account, archive = await asyncio.to_thread(self.mailer.prepare, case, quote)
            with self.repo.tx() as tx:
                current = self.repo.get(case["tenant_id"], case["inquiry_id"], tx)
                clarification_id = case["data"].get("clarification_id") if clarification else None
                same_clarification = (bool(clarification_id) and
                                      current["data"].get("clarification_id") == clarification_id)
                if (current["state"] != expected_state or
                        (current["version"] != job["expected_version"] and not same_clarification)):
                    self.repo.finish(tx, job, "superseded")
                    return {"status": "superseded"}
                current["state"] = "sending"
                current["data"]["dispatch_started"] = True
                current["data"]["outbound_archive"] = archive
                self.repo.write_case(tx, current, current["version"])
            started = True
            receipt = await asyncio.to_thread(sender.send, delivery, account)
            with self.repo.tx() as tx:
                current = self.repo.get(case["tenant_id"], case["inquiry_id"], tx)
                previous = current["version"]
                current["version"] += 1
                current["data"]["dispatch_started"] = False
                target_state = "waiting_customer_info" if clarification else "waiting_customer_feedback"
                if not clarification:
                    sent_at = time.time()
                    current["data"].update(sent_quote_id=quote["artifact_id"], sent_at=sent_at)
                    follow_up_after_send(
                        current["data"], dict(quote["payload"], artifact_id=quote["artifact_id"]), sent_at)
                if current["state"] == "sending":
                    current["state"] = target_state
                elif current["state"] == "needs_message_review":
                    current["data"]["previous_state"] = target_state
                delivery_status = "demo_accepted" if account.get("demo") else "smtp_accepted"
                self.repo.save_artifact(tx, current, "delivery", {"quote_id": quote["artifact_id"], "archive": archive,
                    "status": delivery_status, "receipt": receipt, "quote_hash": quote["content_hash"], "kind": job["kind"],
                    "reply_agent_plan_id": quote["payload"].get("reply_agent", {}).get("plan_id")})
                if not clarification:
                    self.repo.save_artifact(tx, current, "reply_execution", {
                        "plan_id": quote["payload"].get("reply_agent", {}).get("plan_id"),
                        "quote_id": quote["artifact_id"], "email_tool_status": delivery_status,
                        "receipt": receipt, "completed_at": time.time(), "demo_data": bool(account.get("demo"))})
                if current["data"].get("failed_job") == job["kind"]:
                    current["data"].pop("failed_job", None)
                    current["data"].pop("error_code", None)
                self.repo.write_case(tx, current, previous)
                self.repo.event(tx, current, "worker", "smtp_accepted")
                self.repo.finish(tx, job)
                return current
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._fail(job, exc, unknown=started)

    async def run(self):
        while True:
            try:
                result = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.getLogger(__name__).error("Inquiry worker unavailable: %s", type(exc).__name__)
                result = None
            await asyncio.sleep(0.1 if result else 2)


_service = None


def default_service():
    global _service
    if _service is None:
        _service = InquiryService(InquiryRepository())
    return _service


def ingest_envelope(envelope, *, service=None, archive=None, settings=None, mailbox_address="", allowed_senders=(), demo_fixture=False):
    settings = settings or InquirySettings.from_env()
    service = service or default_service()
    archive = archive or (LocalDemoEmailArchive() if settings.demo_mode else S3EmailArchive(settings))
    archived = archive.put(settings.tenant_id, envelope.account_id, envelope.raw_bytes)
    from agent.business.email_trade_classifier import classify_trade_rfq_email
    classification = classify_trade_rfq_email(envelope, mailbox_address=mailbox_address, allowed_senders=allowed_senders)
    ignored = classification.code in {"email_trade_obvious_non_rfq", "email_trade_automated_message", "email_trade_self_sent", "email_trade_sender_not_allowed"}
    return service.ingest(settings.tenant_id, {
        "channel": "email", "external_id": envelope.internet_message_id or envelope.provider_message_id,
        "account_id": envelope.account_id, "sender": envelope.from_address,
        "sender_name": envelope.from_name, "subject": envelope.subject, "text": envelope.text_body,
        "references": list(dict.fromkeys([*envelope.references, *([envelope.thread_id] if envelope.thread_id else [])])),
        "archive": archived, "received_at": envelope.received_at,
        "execution_mode": "demo" if settings.demo_mode else "production",
        "demo_fixture": bool(demo_fixture),
        "ignored": ignored, "intent": "rfq" if classification.accepted else "customer_message",
        "attachments": [asdict(x) for x in envelope.attachments],
    })
