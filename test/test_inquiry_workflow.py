"""No live mailbox, model, customer data, Redis or S3 is used in these tests."""
import asyncio
import copy
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from email.message import EmailMessage
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.access_control.models import Principal
from agent.inquiry.config import InquirySettings
from agent.inquiry.quotation import calculate_final_quote
from agent.inquiry.repository import InquiryError, InquiryRepository
from agent.inquiry.research import DueDiligenceAgent, ResearchWeb
from agent.inquiry.reply import InquiryReplyAgent
from agent.inquiry.runtime import ApprovedMailer, InquiryWorker, QuoteAgent, ingest_envelope
from agent.inquiry.service import InquiryService
from agent.inquiry.storage import LocalDemoEmailArchive, RedisResearchMemory, S3EmailArchive
from providers.base import LLMResponse, ToolCallRequest
from channels.email.mime_parser import parse_email
from channels.inquiry_api import create_inquiry_router

ADMIN = Principal("reviewer", "test", "reviewer", frozenset({"admin"}))
SALES = Principal("sales", "test", "sales", frozenset({"sales"}))
SETTINGS = InquirySettings(tenant_id="test")
PRODUCT = {"sku": "A-1", "name_en": "Widget", "unit": "pcs", "inventory": 500, "moq": 5, "price_usd": 25}
RFQ = {"customer": {"name": "Alice", "company": "Example Ltd"}, "country": "Germany",
       "items": [{"product": "Widget", "quantity": 10, "unit": "pcs"}]}
TERMS = {"recipient": "alice@example.test", "currency": "USD", "payment_terms": "T/T", "trade_term": "FOB Shanghai",
         "delivery": "30 days", "valid_until": "2099-01-01T00:00:00+00:00", "price_basis": "final_net_unit_price",
         "items": [{"sku": "A-1", "quantity": "10", "unit": "pcs", "unit_price": "12.345"}],
         "fees": {"freight": "1.10", "packaging": "0", "tax": "0", "other": "0"}}


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setenv("INQUIRY_WORKFLOW_ENABLED", "true")


@pytest.fixture
def service(tmp_path):
    return InquiryService(InquiryRepository(sqlite_path=tmp_path / "business.sqlite"))


def source(mid="<one>", **overrides):
    return {"channel": "email", "external_id": mid, "sender": "alice@example.test", "sender_name": "Alice",
            "account_id": "mail", "text": "Please quote 10 pcs Widget", "references": [],
            "archive": {"key": "raw/one.eml", "sha256": mid}, **overrides}


def action(service, case, name, payload=None, actor=ADMIN, key=None):
    return service.act(actor, case["inquiry_id"], name, payload or {}, expected_version=case["version"],
                       idempotency_key=key or f"{case['version']}:{name}")


class Catalog:
    def search(self, keyword): return [copy.deepcopy(PRODUCT)]
    def get(self, sku): return copy.deepcopy(PRODUCT)


class Research:
    async def run(self, case, sources, history):
        return {"summary": "Needs identity review", "gaps": ["Authority unknown"], "evidence": sources,
                "company_contacts": {"email": [{"value": "public@example.test", "evidence_ids": [sources[0]["message_id"]]}]}}


class Mailer:
    def __init__(self, fail=False): self.sent, self.fail = [], fail
    def prepare(self, case, artifact):
        return self, {"artifact_id": artifact["artifact_id"]}, {}, {"bucket": "test", "key": "outbound.eml"}
    def send(self, delivery, account):
        self.sent.append(delivery)
        if self.fail: raise TimeoutError()
        return "<accepted-by-smtp>"


async def extractor(text, context): return copy.deepcopy(RFQ)


def worker(service, **kwargs):
    return InquiryWorker(service, settings=SETTINGS, extractor=extractor, research=Research(),
                         quotation=QuoteAgent(Catalog()), mailer=kwargs.pop("mailer", Mailer()), **kwargs)


def step(service, **kwargs): return asyncio.run(worker(service, **kwargs).run_once())


def at_quote(service):
    case = service.ingest("test", source())
    case = step(service)
    assert case["state"] == "waiting_rfq_review"
    case = action(service, case, "review_rfq", {"rfq": RFQ, "customer_type": "company"})
    case = step(service)
    assert case["state"] == "waiting_due_diligence_review"
    case = action(service, case, "approve_due_diligence", {"reason": "Verified contact by phone"})
    case = step(service)
    assert case["state"] == "waiting_negotiation"
    case = action(service, case, "submit_terms", {"terms": TERMS}, SALES)
    case = step(service)
    assert case["state"] == "composing_reply"
    case = step(service)
    assert case["state"] == "waiting_quote_approval"
    return case


def test_full_workflow_restart_pause_resume_and_structured_acceptance(service):
    case = at_quote(service)
    restarted = InquiryService(InquiryRepository(sqlite_path=service.repo.path))
    case = action(restarted, case, "approve_quote")
    mailer = Mailer()
    case = step(restarted, mailer=mailer)
    assert case["state"] == "waiting_customer_feedback"
    assert len(mailer.sent) == 1
    assert step(restarted, mailer=mailer) is None
    sent_id = case["data"]["sent_quote_id"]
    quote = next(x for x in restarted.repo.artifacts("test", case["inquiry_id"]) if x["artifact_id"] == sent_id)
    reply = source("<yes>", text="We accept the quotation without changes", references=[quote["payload"]["smtp_message_id"]])
    case = restarted.ingest("test", reply)
    assert case["state"] == "needs_message_review"
    case = action(restarted, case, "classify_message", {"intent": "acceptance"})
    evidence = restarted.messages("test", case["inquiry_id"])[-1]["message_id"]
    case = action(restarted, case, "confirm_acceptance", {"quote_id": sent_id, "message_ids": [evidence]})
    assert case["state"] == "accepted"
    records = restarted.repo.artifacts("test", case["inquiry_id"])
    report = next(x["payload"] for x in records if x["kind"] == "report")
    accepted = next(x["payload"] for x in records if x["kind"] == "acceptance")
    assert report["customer_id"] == case["customer_id"] == accepted["customer_id"]
    assert accepted["agreed_quote"]["total"] == "124.55"
    assert accepted["agreed_quote"]["items"][0]["unit_price"] == "12.345"
    assert accepted["quote_id"] == sent_id and accepted["source_message_ids"] == [evidence]


def test_idempotent_ingest_and_stable_customer_identity(service):
    one = service.ingest("test", source())
    assert service.ingest("test", source())["inquiry_id"] == one["inquiry_id"]
    two = service.ingest("test", source("<two>"))
    assert one["inquiry_id"] != two["inquiry_id"] and one["customer_id"] == two["customer_id"]
    other = service.ingest("another", source())
    assert other["customer_id"] != one["customer_id"]
    assert len(service.messages("test", one["inquiry_id"])) == 1


def test_demo_and_production_sources_cannot_mix_in_one_inquiry(service):
    case = service.ingest("test", source(execution_mode="demo"))
    with pytest.raises(InquiryError, match="execution_mode_mismatch"):
        service.ingest("test", source("<reply>", references=["<one>"]))
    assert service.repo.get("test", case["inquiry_id"])["data"]["execution_mode"] == "demo"


def test_local_demo_archive_persists_the_exact_rfc822_bytes(tmp_path):
    archive = LocalDemoEmailArchive(tmp_path / "demo-mailbox")
    raw = b"From: alice@example.test\r\nSubject: RFQ\r\n\r\nOriginal bytes\r\n"
    stored = archive.put("test", "account", raw)
    assert stored["provider"] == "local_demo_mailbox" and stored["demo_data"] is True
    assert (tmp_path / "demo-mailbox" / stored["key"]).read_bytes() == raw


def test_demo_mailer_persists_the_approved_reply_as_a_local_eml(service, tmp_path):
    case = action(service, at_quote(service), "approve_reply")
    artifact = next(item for item in service.repo.artifacts("test", case["inquiry_id"])
                    if item["artifact_id"] == case["data"]["quote_id"])
    archive = LocalDemoEmailArchive(tmp_path / "demo-mailbox")
    mailer = ApprovedMailer(replace(SETTINGS, demo_mode=True), archive)
    sender, delivery, account, stored = mailer.prepare(case, artifact)
    receipt = sender.send(delivery, account)
    assert receipt.startswith("<demo-accepted-") and account["demo"] is True
    assert (tmp_path / "demo-mailbox" / stored["key"]).read_bytes() == delivery["original_bytes"]


def test_reply_agent_uses_a_bounded_model_decision_but_keeps_price_deterministic(service):
    class Model:
        def __init__(self): self.calls = []
        async def chat(self, messages, tools=None, model=None):
            self.calls.append((messages, tools))
            if tools:
                return LLMResponse(tool_calls=[ToolCallRequest("one", "choose_reply_strategy", {
                    "strategy": "compose_quote_reply", "reason": "Confirmed quote covers the request."})])
            return LLMResponse(content="Thank you for your inquiry. We are pleased to share the approved quotation.")
    case = at_quote(service)
    model = Model()
    draft = asyncio.run(InquiryReplyAgent(Catalog(), provider=model).compose(
        service.repo.get("test", case["inquiry_id"]), service.messages("test", case["inquiry_id"])))
    assert len(model.calls) == 2
    assert draft["total"] == "124.55" and "124.55" in draft["body"]
    assert draft["reply_agent"]["tool_trace"][-2]["tool"] == "quote_calculator"


def test_editing_reply_draft_creates_a_new_version_and_requires_new_approval(service):
    case = at_quote(service)
    artifact = next(item for item in service.repo.artifacts("test", case["inquiry_id"])
                    if item["artifact_id"] == case["data"]["quote_id"])
    case = action(service, case, "edit_reply_draft", {
        "subject": "Re: revised quotation", "body": artifact["payload"]["body"] + "\n\nEdited by sales.",
    }, SALES)
    drafts = [item for item in service.repo.artifacts("test", case["inquiry_id"]) if item["kind"] == "quote"]
    assert len(drafts) == 2 and case["data"]["quote_id"] == drafts[-1]["artifact_id"]
    assert "quote_approval" not in case["data"] and drafts[-1]["payload"]["edited_from"] == drafts[0]["artifact_id"]


def test_idempotency_conflicts_and_version_guard(service):
    case = service.ingest("test", source())
    result = action(service, case, "close", {"reason": "duplicate"}, key="same")
    assert action(service, case, "close", {"reason": "duplicate"}, key="same") == result
    with pytest.raises(InquiryError, match="idempotency_key_reused"):
        action(service, case, "close", {"reason": "different"}, key="same")
    with pytest.raises(InquiryError, match="version_conflict"):
        action(service, case, "close", {"reason": "duplicate"}, key="new")


def test_new_message_supersedes_worker_and_repeated_messages_keep_previous_state(service):
    case = service.ingest("test", source())
    case = service.ingest("test", source("<two>", references=["<one>"]))
    case = service.ingest("test", source("<three>", references=["<one>"]))
    assert case["data"]["previous_state"] == "extracting"
    assert step(service)["status"] == "superseded"
    case = action(service, case, "classify_message", {"intent": "unrelated"})
    assert case["state"] == "extracting"
    assert step(service)["state"] == "waiting_rfq_review"


def test_review_gates_and_tenant_isolation(service):
    case = service.ingest("test", source())
    with pytest.raises(InquiryError, match="transition_not_allowed"):
        action(service, case, "approve_quote")
    case = at_quote(InquiryService(InquiryRepository(sqlite_path=service.repo.path + "second")))
    with pytest.raises(InquiryError, match="forbidden"):
        action(service, case, "approve_quote", actor=SALES)
    other = Principal("other", "another", "other", frozenset({"admin"}))
    first = service.repo.list("test")[0]
    with pytest.raises(InquiryError, match="not_found"):
        action(service, first, "close", {"reason": "x"}, actor=other)


def test_missing_identity_cannot_start_research(service):
    service.ingest("test", source())
    case = step(service)
    incomplete = copy.deepcopy(RFQ)
    incomplete["customer"].pop("company")
    incomplete["items"][0]["quantity"] = "NaN"
    case = action(service, case, "review_rfq", {"rfq": incomplete, "customer_type": "company"})
    assert case["state"] == "waiting_customer_info"
    assert "customer.company" in case["data"]["gaps"]
    assert "items[0].quantity" in case["data"]["gaps"]


def test_clarification_send_and_reply_resume(service):
    service.ingest("test", source())
    case = step(service)
    case = action(service, case, "review_rfq", {"rfq": {"customer": {"name": "Alice"}, "items": []}, "customer_type": "individual"})
    case = action(service, case, "request_clarification", {"body": "Please provide products, quantities and units."})
    mailer = Mailer()
    case = step(service, mailer=mailer)
    assert case["state"] == "waiting_customer_info" and not case["data"].get("sent_quote_id")
    clarification = next(x for x in service.repo.artifacts("test", case["inquiry_id"]) if x["kind"] == "clarification")
    case = service.ingest("test", source("<more>", references=[clarification["payload"]["smtp_message_id"]]))
    assert case["state"] == "extracting"
    assert step(service)["state"] == "waiting_rfq_review"


def test_smtp_uncertainty_never_auto_resends(service):
    case = action(service, at_quote(service), "approve_quote")
    mailer = Mailer(fail=True)
    assert step(service, mailer=mailer)["status"] == "delivery_unknown"
    current = service.repo.get("test", case["inquiry_id"])
    with pytest.raises(InquiryError, match="transition_not_allowed"):
        action(service, current, "retry")
    assert step(service, mailer=mailer) is None and len(mailer.sent) == 1
    current = action(service, current, "resolve_delivery", {"outcome": "not_sent", "reason": "SMTP logs show rejected"})
    assert current["state"] == "waiting_quote_approval"
    assert any(a["kind"] == "delivery_reconciliation" for a in service.repo.artifacts("test", case["inquiry_id"]))


def test_crashed_send_lease_requires_reconciliation(service):
    case = action(service, at_quote(service), "approve_quote")
    job = service.repo.claim("test")
    with service.repo.tx() as tx:
        c = service.repo.get("test", case["inquiry_id"], tx)
        c["state"], c["data"]["dispatch_started"] = "sending", True
        service.repo.write_case(tx, c, c["version"])
        tx.execute("UPDATE inquiry_job SET lease_until=0 WHERE job_id=?", (job["job_id"],)).close()
    case = service.ingest("test", source("<changed>", references=["<one>"]))
    mailer = Mailer()
    assert step(service, mailer=mailer)["status"] == "delivery_unknown"
    assert mailer.sent == []


@pytest.mark.parametrize("field,value,code", [("quantity", "0", "number_invalid"), ("quantity", "NaN", "number_invalid"),
    ("quantity", "1000", "insufficient_inventory"), ("quantity", "1", "below_moq"), ("unit", "kg", "unit_mismatch"),
    ("unit_price", "-2", "number_invalid"), ("unit_price", True, "number_invalid")])
def test_quote_rejects_invalid_terms(field, value, code):
    terms = copy.deepcopy(TERMS)
    terms["items"][0][field] = value
    with pytest.raises(InquiryError, match=code): calculate_final_quote(terms, [PRODUCT])


def test_quote_fees_and_price_basis_must_be_explicit():
    for field, code in [("fees", "explicit_fees"), ("price_basis", "price_basis")]:
        terms = copy.deepcopy(TERMS)
        terms.pop(field)
        with pytest.raises(InquiryError, match=code): calculate_final_quote(terms, [PRODUCT])
    result = calculate_final_quote(TERMS, [PRODUCT])
    assert result["subtotal"] == "123.45" and result["total"] == "124.55"


def test_s3_preserves_exact_bytes_and_scopes_objects():
    class S3:
        def __init__(self): self.requests = []
        def put_object(self, **kwargs): self.requests.append(kwargs); return {"VersionId": "v1"}
    client = S3()
    archive = S3EmailArchive(replace(SETTINGS, s3_bucket="bucket"), client)
    raw = b"From: alice@example.test\r\nSubject: RFQ\r\n\r\nOriginal\x00bytes\r\n"
    a = archive.put("test", "account", raw)
    b = archive.put("other", "account", raw)
    assert client.requests[0]["Body"] == raw and a["key"] != b["key"]
    assert client.requests[0]["ServerSideEncryption"] == "AES256" and a["version_id"] == "v1"


def test_archive_failure_prevents_business_ingest(service):
    raw = b"From: alice@example.test\r\nSubject: RFQ\r\n\r\nPlease quote 10 pcs Widget."
    envelope = parse_email(raw, account_id="mail", provider="import", folder="INBOX", uidvalidity=1, uid=1)
    assert envelope.raw_bytes == raw and "raw_bytes" not in envelope.to_dict()
    with pytest.raises(InquiryError, match="s3_bucket_not_configured"):
        ingest_envelope(envelope, service=service, settings=SETTINGS)
    assert service.repo.list("test") == []


def test_redis_ttl_and_inquiry_revision_isolation():
    class Redis:
        def __init__(self): self.values, self.ttls = {}, []
        async def get(self, key): return self.values.get(key)
        async def set(self, key, value, ex): self.values[key] = value; self.ttls.append(ex)
    client = Redis()
    memory = RedisResearchMemory(SETTINGS, client)
    async def scenario():
        await memory.save("test", "case", 1, {"steps": 2})
        assert await memory.load("test", "case", 1) == {"steps": 2}
        assert await memory.load("test", "case", 2) is None
        assert await memory.load("other", "case", 1) is None
    asyncio.run(scenario())
    assert client.ttls == [86400]


def test_research_citations_claims_contacts_and_malformed_output():
    evidence = [{"evidence_id": "m1", "source_type": "customer_message"}]
    report = {"facts": [{"field": "company", "value": "ABC", "status": "supported", "evidence_ids": ["m1"]},
                        {"field": "revenue", "value": "1 billion", "status": "supported", "evidence_ids": ["fake"]}],
              "company_contacts": {"email": [{"value": "a@b.test", "evidence_ids": ["m1"]}], "phone": None}}
    result = DueDiligenceAgent.validate(report, evidence, "company")
    assert len(result["facts"]) == 1 and result["facts"][0]["status"] == "customer_provided"
    assert result["company_contacts"]["email"][0]["value"] == "a@b.test"
    assert result["review_status"] == "pending_human_review" and result["gaps"]
    result = DueDiligenceAgent.validate({"facts": None, "company_contacts": {"email": [1, {"value": "x", "evidence_ids": [{}]}]}}, evidence, "company")
    assert result["facts"] == [] and result["company_contacts"]["email"] == []


def test_research_restores_finished_redis_result_without_model():
    class Memory:
        async def load(self, *args): return {"report": {"summary": "saved"}}
    class Provider:
        async def chat(self, **kwargs): raise AssertionError("must not repeat")
    agent = DueDiligenceAgent(Provider(), Memory(), None)
    case = {"tenant_id": "test", "inquiry_id": "i", "data": {"research_revision": 1}}
    assert asyncio.run(agent.run(case, [], [])) == {"summary": "saved"}


@pytest.mark.parametrize("url", ["http://127.0.0.1", "http://169.254.169.254/latest/meta-data", "file:///etc/passwd", "http://user:pass@example.com", "http://[::1]"])
def test_crawler_rejects_nonpublic_targets(url):
    with pytest.raises(InquiryError): ResearchWeb._fetch(url)


def test_api_honors_actor_roles_and_version(service):
    case = service.ingest("test", source())
    app = FastAPI()
    app.include_router(create_inquiry_router(lambda request: ADMIN, lambda: service))
    with TestClient(app) as client:
        assert client.get("/api/inquiries").json()["items"][0]["customer_id"] == case["customer_id"]
        assert client.post(f"/api/inquiries/{case['inquiry_id']}/actions", json={"action": "close", "expected_version": 1, "payload": {"reason": "x"}}).status_code == 422
        r = client.post(f"/api/inquiries/{case['inquiry_id']}/actions", headers={"Idempotency-Key": "close"}, json={"action": "close", "expected_version": 1, "payload": {"reason": "x"}})
        assert r.status_code == 200
    app = FastAPI()
    app.include_router(create_inquiry_router(lambda request: Principal("customer", "test", "customer", frozenset({"customer"})), lambda: service))
    with TestClient(app) as client: assert client.get("/api/inquiries").status_code == 403


def test_configured_automatic_clarification_is_bounded(service):
    async def missing(text, context): return {"customer": {"name": "Alice"}, "items": []}
    w = InquiryWorker(service, settings=replace(SETTINGS, auto_clarify=True), extractor=missing, mailer=Mailer())
    case = service.ingest("test", source())
    for index in range(3):
        case = asyncio.run(w.run_once())
        assert case["state"] == "clarify_queued"
        case = asyncio.run(w.run_once())
        assert case["state"] == "waiting_customer_info"
        case = service.ingest("test", source(f"<reply-{index}>", references=["<one>"]))
    case = asyncio.run(w.run_once())
    assert case["state"] == "waiting_customer_info" and case["data"]["auto_clarify_count"] == 3
    assert "customer.company" in case["data"]["gaps"]
    assert len(w.mailer.sent) == 3


def test_automatic_email_flow_never_waits_for_manual_rfq_review(service):
    w = InquiryWorker(service, settings=replace(SETTINGS, auto_clarify=True),
                      extractor=extractor, research=Research(), mailer=Mailer())
    service.ingest("test", source())
    case = asyncio.run(w.run_once())
    assert case["state"] == "researching"
    assert case["data"]["gaps"] == []
    assert asyncio.run(w.run_once())["state"] == "waiting_due_diligence_review"


def test_automatic_clarification_can_only_reply_to_original_sender(service):
    async def missing(text, context):
        return {"customer": {"name": "Alice"}, "country": "Germany", "items": []}

    settings = replace(SETTINGS, auto_clarify=True, smtp_enabled=True)
    workflow = InquiryWorker(service, settings=settings, extractor=missing, mailer=Mailer())
    service.ingest("test", source())
    case = asyncio.run(workflow.run_once())
    artifact = next(item for item in service.repo.artifacts("test", case["inquiry_id"])
                    if item["kind"] == "clarification")
    account = {"account_id": "mail", "outbound_enabled": True, "status": "healthy",
               "allowed_recipients": [], "address": "sales@example.test"}

    class Archive:
        def put(self, tenant, account_id, raw):
            return {"key": "out.eml"}

    class Sender:
        def build_message(self, delivery, selected_account):
            message = EmailMessage()
            message["From"] = selected_account["address"]
            message["To"] = delivery["recipient"]
            message["Subject"] = delivery["subject_snapshot"]
            message.set_content(delivery["body_snapshot"])
            return message

    mailer = ApprovedMailer(settings, Archive(),
                            SimpleNamespace(get=lambda _: account), Sender())
    _, delivery, _, _ = mailer.prepare(case, artifact)
    assert delivery["recipient"] == "alice@example.test"

    redirected = copy.deepcopy(artifact)
    redirected["payload"]["recipient"] = "attacker@example.test"
    with pytest.raises(InquiryError, match="clarification_recipient_mismatch"):
        mailer.prepare(case, redirected)


def test_research_tool_loop_keeps_cited_evidence_and_redis_context():
    class Memory:
        def __init__(self): self.value = None
        async def load(self, *args): return self.value
        async def save(self, *args): self.value = copy.deepcopy(args[-1])
    class Web:
        async def official_website(self, query):
            return {"status": "ok", "pages": [{"url": "https://example.test", "status": "ok", "sha256": "a" * 64,
                "text": "Example Ltd Germany Example Street 1 +49 30 1234567"}]}
        async def map_lookup(self, query):
            return {"status": "ok", "results": [{"id": "place-1", "websiteUri": "https://example.test",
                "formattedAddress": "Example Street 1", "nationalPhoneNumber": "+49 30 1234567", "displayName": "Example Ltd Germany"}]}
        async def customs_lookup(self, query):
            return {"status": "ok", "data_level": "company_shipment", "results": [{"record_id": "shipment-1", "company": "Example Ltd"}]}
        async def news_search(self, query):
            return {"status": "ok", "results": [{"url": "https://news.example.test/example", "evidence_quality": "page_fetched", "sha256": "b" * 64, "text": "Example Ltd news"}]}
    class Provider:
        def __init__(self): self.calls = 0
        async def chat(self, messages):
            self.calls += 1
            system = messages[0]["content"]
            if "计划" in system:
                result = {"objective": "Verify the supplied company", "steps": []}
            elif "检查器" in system:
                result = {"complete": True, "gaps": [], "follow_up_steps": []}
            else:
                result = {"summary": "Needs approval", "facts": [], "company_contacts": {},
                          "inquiry_contact": {}, "gaps": [], "conflicts": []}
            return SimpleNamespace(content=json.dumps(result))
    memory = Memory()
    agent = DueDiligenceAgent(Provider(), memory, Web())
    rfq = copy.deepcopy(RFQ)
    rfq["customer"].update(website="https://example.test", address="Example Street 1", phone="+49 30 1234567")
    case = {"inquiry_id": "i", "customer_id": "c", "tenant_id": "test", "data": {"research_revision": 1, "rfq": rfq, "customer_type": "company"}}
    report = asyncio.run(agent.run(case, [], []))
    assert len(report["evidence"]) == 4 and memory.value["steps"] == 4
    assert report["company_identity"]["status"] == "verified"
    assert report["review_status"] == "pending_human_review"


def test_outbound_archive_and_smtp_use_identical_mime_and_approval(service):
    from channels.email.smtp_sender import SmtpSslSender
    case = action(service, at_quote(service), "approve_quote")
    artifact = next(a for a in service.repo.artifacts("test", case["inquiry_id"]) if a["kind"] == "quote")
    account = {"account_id": "mail", "outbound_enabled": True, "status": "healthy", "allowed_recipients": [TERMS["recipient"]],
               "provider": "qq", "sender_name": "Sales", "address": "sales@example.test", "secret_ref": "fake"}
    class Archive:
        def put(self, tenant, account, raw): self.raw = raw; return {"key": "out.eml"}
    class SMTP:
        def __init__(self, *args, **kwargs): pass
        def login(self, *args): pass
        def sendmail(self, sender, recipients, raw): self.__class__.raw = raw; return {}
        def quit(self): pass
    archive = Archive()
    sender = SmtpSslSender(SimpleNamespace(get=lambda _: "fake"), smtp_factory=SMTP)
    mailer = ApprovedMailer(replace(SETTINGS, smtp_enabled=True), archive,
                            SimpleNamespace(get=lambda _: account), sender)
    engine, delivery, selected, location = mailer.prepare(case, artifact)
    engine.send(delivery, selected)
    assert SMTP.raw == archive.raw == delivery["original_bytes"]
    assert b'filename="quotation.html"' in SMTP.raw and b"\r\n" in SMTP.raw
    account["allowed_recipients"] = []
    with pytest.raises(InquiryError, match="recipient_not_allowed"): mailer.prepare(case, artifact)
    account["allowed_recipients"] = [TERMS["recipient"]]
    case["data"]["quote_approval"]["hash"] = "modified"
    with pytest.raises(InquiryError, match="approval_stale"): mailer.prepare(case, artifact)


def test_mailbox_cursor_advances_only_after_s3_and_db(service):
    from agent.inquiry.mailbox import InquiryMailboxPoller
    raw = b"From: alice@example.test\r\nMessage-ID: <original>\r\nSubject: RFQ\r\n\r\nPlease quote 10 pcs Widget."
    envelope = parse_email(raw, account_id="mail", provider="qq", folder="INBOX", uidvalidity=7, uid=10)
    account = {"account_id": "mail", "provider": "qq", "address": "sales@example.test", "secret_ref": "fake", "folder": "INBOX",
               "inbound_enabled": True, "status": "healthy", "poll_seconds": 30}
    class Archive:
        fail = True
        def put(self, tenant, account, value):
            assert value == raw
            if self.fail: raise InquiryError("email_s3_archive_failed")
            return {"key": "raw.eml", "sha256": "hash"}
    class Source:
        def __init__(self, *args): pass
        def fetch_after(self, *args): return [envelope]
    archive = Archive()
    poller = InquiryMailboxPoller(service=service, settings=SETTINGS, accounts=SimpleNamespace(list_active=lambda: [account]),
                                 secrets=SimpleNamespace(get=lambda _: "fake"), source_factory=Source, archive=archive)
    assert asyncio.run(poller.poll_due())[0]["status"] == "error"
    with service.repo.tx() as tx: assert tx.one("SELECT * FROM inquiry_mail_cursor") is None
    archive.fail = False
    poller.next_due.clear()
    assert asyncio.run(poller.poll_due())[0]["status"] == "polled"
    with service.repo.tx() as tx: assert tx.one("SELECT * FROM inquiry_mail_cursor")["last_uid"] == 10
    assert len(service.repo.list("test")) == 1


def test_acceptance_rejects_wrong_sender_and_unsent_version(service):
    case = action(service, at_quote(service), "approve_quote")
    case = step(service)
    case = service.ingest("test", source("<intruder>", sender="someone@example.test", references=["<one>"]))
    case = action(service, case, "classify_message", {"intent": "acceptance"})
    mid = service.messages("test", case["inquiry_id"])[-1]["message_id"]
    with pytest.raises(InquiryError, match="quote_mismatch"):
        action(service, case, "confirm_acceptance", {"quote_id": "wrong", "message_ids": [mid]})
    with pytest.raises(InquiryError, match="sender_mismatch"):
        action(service, case, "confirm_acceptance", {"quote_id": case["data"]["sent_quote_id"], "message_ids": [mid]})
    assert service.repo.get("test", case["inquiry_id"])["state"] == "waiting_acceptance_review"


def test_new_inbound_during_smtp_keeps_reply_pending(service):
    case = action(service, at_quote(service), "approve_quote")
    class IncomingMailer(Mailer):
        def send(self, delivery, account):
            service.ingest("test", source("<while-sending>", references=["<one>"]))
            return "<smtp-receipt>"
    current = step(service, mailer=IncomingMailer())
    assert current["state"] == "needs_message_review"
    assert current["data"]["previous_state"] == "waiting_customer_feedback"
    assert current["data"]["sent_quote_id"] == case["data"]["quote_id"]


def test_old_quote_tool_is_blocked_when_workflow_enabled():
    from agent.tools.create_quote import create_quote_version_impl
    import inspect
    # Invoke the actual legacy entry, which must gate before looking up RFQ data.
    args = {name: 1 for name, p in inspect.signature(create_quote_version_impl).parameters.items()
            if p.default is inspect.Parameter.empty}
    result = json.loads(create_quote_version_impl(**args))
    assert result["error"] == "inquiry_workflow_required"
