"""Human-controlled state transitions; every entry point uses these guards."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from copy import deepcopy

from agent.access_control.models import Principal
from .config import InquirySettings
from .repository import InquiryError, InquiryRepository, digest, encode, new_id


STAFF = {"admin", "employee", "sales", "quote_reviewer"}
REVIEWERS = {"admin", "quote_reviewer"}
EMAIL = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")


def clarification_body(gaps, language="en"):
    names = ({"product": "产品或型号", "quantity": "采购数量", "unit": "数量单位",
              "items": "所需产品、数量和单位", "country": "目的国家或地区",
              "customer.name_or_company": "联系人或公司名称", "customer.company": "公司名称"}
             if language == "zh" else
             {"product": "product/model", "quantity": "quantity", "unit": "quantity unit",
              "items": "requested products, quantities and units", "country": "country / region",
              "customer.name_or_company": "contact name or company name", "customer.company": "company name"})
    questions = []
    for gap in gaps:
        match = re.fullmatch(r"items\[(\d+)\]\.(\w+)", gap)
        if match:
            item_number = int(match[1]) + 1
            item_label = f"第{item_number}项" if language == "zh" else f"Item {item_number}"
            questions.append(f"{item_label}: {names.get(match[2], match[2])}")
        else:
            questions.append(names.get(gap, gap))
    if language == "zh":
        return "您好：\n\n为了准确准备报价，请补充以下信息：\n" + "\n".join("- " + x for x in questions) + "\n\n谢谢。"
    return "Hello,\n\nTo prepare your quotation, please provide:\n" + "\n".join("- " + x for x in questions) + "\n\nThank you."


def require_actor(actor, *, review=False):
    if not isinstance(actor, Principal) or "customer" in actor.roles or not actor.roles.intersection(REVIEWERS if review else STAFF):
        raise InquiryError("inquiry_action_forbidden", 403)


def value(node):
    return node.get("value") if isinstance(node, dict) and "value" in node else node


def normalized_rfq(raw):
    customer = raw.get("customer") or {}
    items = []
    for row in raw.get("items") or []:
        quantity = row.get("quantity") or {}
        items.append({"product": value(row.get("product")) or "", "specification": value(row.get("specification")) or "",
                      "quantity": value(quantity), "unit": quantity.get("unit", "") if isinstance(quantity, dict) else row.get("unit", "")})
    return {"customer": {k: value(v) for k, v in customer.items()}, "country": value(raw.get("country")),
            "items": items, "trade_term": raw.get("trade_term"), "raw_extraction": raw}


def rfq_gaps(rfq):
    gaps = []
    if not isinstance(rfq, dict) or not isinstance(rfq.get("customer", {}), dict) or not isinstance(rfq.get("items", []), list):
        raise InquiryError("rfq_structure_invalid", 422)
    if not rfq.get("items"):
        gaps.append("items")
    for i, row in enumerate(rfq.get("items") or []):
        if not isinstance(row, dict):
            raise InquiryError("rfq_structure_invalid", 422)
        for key in ("product", "quantity", "unit"):
            if not row.get(key):
                gaps.append(f"items[{i}].{key}")
        if row.get("quantity"):
            from .quotation import number
            try:
                number(row["quantity"], positive=True)
            except InquiryError:
                gaps.append(f"items[{i}].quantity")
    if not rfq.get("customer", {}).get("name") and not rfq.get("customer", {}).get("company"):
        gaps.append("customer.name_or_company")
    if not rfq.get("country"):
        gaps.append("country")
    return gaps


def quote_entry_gaps(rfq):
    """Fields required before product lookup; quantity and price are entered later."""
    if not isinstance(rfq, dict) or not isinstance(rfq.get("items"), list):
        return ["items"]
    gaps = []
    if not rfq["items"]:
        gaps.append("items")
    for index, item in enumerate(rfq["items"]):
        if not isinstance(item, dict) or not str(item.get("product") or "").strip():
            gaps.append(f"items[{index}].product")
    return gaps


def follow_up_after_send(data, quote, sent_at):
    """Create the next actionable sales follow-up after SMTP acceptance."""
    previous = data.get("follow_up") if isinstance(data.get("follow_up"), dict) else {}
    sequence = int(quote.get("follow_up_sequence") or previous.get("sequence") or 0)
    due_at = datetime.fromtimestamp(sent_at + 3 * 24 * 60 * 60, timezone.utc).isoformat()
    follow_up = {
        "status": "scheduled",
        "due_at": due_at,
        "owner": str(previous.get("owner") or data.get("terms_confirmed_by") or "sales"),
        "note": str(previous.get("note") or "")[:4000],
        "sequence": sequence,
        "last_contact_at": sent_at,
    }
    data["follow_up"] = follow_up
    data["next_follow_up_at"] = due_at
    data["follow_up_count"] = max(int(data.get("follow_up_count") or 0), sequence)
    data.setdefault("initial_sent_quote_id", quote.get("artifact_id") or "")
    return follow_up


class InquiryService:
    def __init__(self, repository: InquiryRepository):
        self.repo = repository

    def delete_cases(self, actor, items):
        require_actor(actor, review=True)
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise InquiryError("case_selection_required", 422)
        ids = [item.get("inquiry_id") for item in items if isinstance(item, dict)]
        if len(ids) != len(items) or len(set(ids)) != len(ids):
            raise InquiryError("case_selection_invalid", 422)
        with self.repo.tx() as tx:
            for item in sorted(items, key=lambda row: row["inquiry_id"]):
                case = self.repo.get(actor.tenant_id, item["inquiry_id"], tx)
                if case["version"] != item.get("expected_version"):
                    raise InquiryError("inquiry_version_conflict", 409)
                jobs = tx.all(
                    "SELECT status FROM inquiry_job WHERE tenant_id=? AND inquiry_id=? "
                    "AND status IN ('pending','running')" + (" FOR UPDATE" if tx.mysql else ""),
                    (actor.tenant_id, case["inquiry_id"]),
                )
                if any(job["status"] == "running" for job in jobs) or case["state"] in {"sending", "delivery_unknown"}:
                    raise InquiryError("case_delete_busy", 409)
            self.repo.delete_cases(tx, actor.tenant_id, ids)
        return {"deleted_ids": ids, "deleted_count": len(ids)}

    def ingest(self, tenant, message, *, inquiry_id=None):
        """message is a server-built normalized source, never a workflow decision."""
        channel = message.get("channel")
        if channel not in {"email", "whatsapp"}:
            raise InquiryError("source_channel_invalid", 422)
        if channel == "email" and not message.get("archive", {}).get("key"):
            raise InquiryError("email_original_not_archived", 422)
        if not message.get("sender") or not message.get("external_id"):
            raise InquiryError("source_identity_required", 422)
        if len(message.get("text", "")) > 100000:
            raise InquiryError("source_text_too_large", 413)
        execution_mode = str(message.get("execution_mode") or "production")
        if execution_mode not in {"production", "demo"}:
            raise InquiryError("source_execution_mode_invalid", 422)
        source_key = digest([channel, message.get("account_id"), message["external_id"],
                             message.get("archive", {}).get("sha256", digest(message.get("text", "")))])
        with self.repo.tx() as tx:
            duplicate = tx.one("SELECT inquiry_id FROM inquiry_message WHERE tenant_id=? AND source_key=?", (tenant, source_key))
            if duplicate:
                return self.repo.get(tenant, duplicate["inquiry_id"], tx) if duplicate["inquiry_id"] else {"state": "ignored"}
            if message.get("ignored"):
                tx.execute("INSERT INTO inquiry_message VALUES(?,?,?,?,?,?,?,?)",
                           (new_id("msg"), tenant, source_key, None, channel, message["external_id"], encode(message), time.time())).close()
                return {"state": "ignored"}
            # Only unique thread matches in the same mailbox/channel are linked.
            references = message.get("references") or []
            matches = set()
            for reference in references[:30]:
                for row in tx.all("SELECT inquiry_id,payload_json FROM inquiry_message WHERE tenant_id=? AND channel=? AND external_id=?",
                                  (tenant, channel, reference)):
                    previous = json.loads(row["payload_json"])
                    if previous.get("account_id") == message.get("account_id") and row["inquiry_id"]:
                        matches.add(row["inquiry_id"])
                # Outgoing messages are represented by immutable quote snapshots.
                for row in tx.all("SELECT inquiry_id,payload_json FROM inquiry_artifact WHERE tenant_id=? AND kind IN ('quote','clarification')", (tenant,)):
                    quote = json.loads(row["payload_json"])
                    if quote.get("smtp_message_id") == reference and quote.get("account_id") == message.get("account_id"):
                        matches.add(row["inquiry_id"])
            if not inquiry_id and len(matches) == 1:
                inquiry_id = next(iter(matches))
            mid = new_id("msg")
            if inquiry_id:
                case = self.repo.get(tenant, inquiry_id, tx)
                if case["data"].get("execution_mode", "production") != execution_mode:
                    raise InquiryError("inquiry_execution_mode_mismatch", 409)
                previous = case["version"]
                case["version"] += 1
                case["data"].setdefault("pending_message_ids", []).append(mid)
                if case["state"] == "waiting_customer_feedback":
                    follow_up = case["data"].get("follow_up")
                    if isinstance(follow_up, dict):
                        follow_up.update(status="customer_replied", replied_at=time.time())
                # New customer material never silently overwrites reviewed terms.
                if case["state"] == "waiting_customer_info":
                    case["state"] = "extracting"
                    self.repo.enqueue(tx, case, "extract")
                else:
                    if case["state"] != "needs_message_review":
                        case["data"]["previous_state"] = case["state"]
                    case["state"] = "needs_message_review"
                self.repo.write_case(tx, case, previous)
            else:
                identity_key = digest([channel, message["sender"].strip().casefold()])
                customer = tx.one("SELECT * FROM inquiry_customer WHERE tenant_id=? AND identity_key=?", (tenant, identity_key))
                if customer:
                    cid = customer["customer_id"]
                else:
                    cid = new_id("CUS")
                    profile = {"type": "unknown", "name": message.get("sender_name", ""),
                               "contacts": [{"channel": channel, "value": message["sender"], "status": "customer_provided"}]}
                    tx.execute("INSERT INTO inquiry_customer VALUES(?,?,?,?,?)",
                               (cid, tenant, identity_key, encode(profile), time.time())).close()
                requires_intent_review = message.get("intent") == "unknown"
                case = {"inquiry_id": new_id("INQ"), "tenant_id": tenant, "customer_id": cid,
                        "state": "needs_message_review" if requires_intent_review else "extracting", "version": 1,
                        "data": {"message_ids": [], "pending_message_ids": [mid] if requires_intent_review else [], "account_id": message.get("account_id", ""),
                                 "recipient": message["sender"] if channel == "email" else "",
                                 "customer_type": "unknown", "research_revision": 1,
                                 "execution_mode": execution_mode,
                                 "demo_fixture": bool(message.get("demo_fixture"))}}
                tx.execute("INSERT INTO inquiry_case VALUES(?,?,?,?,?,?,?,?)",
                           (case["inquiry_id"], tenant, cid, case["state"], 1, encode(case["data"]), time.time(), time.time())).close()
                if case["state"] == "extracting":
                    self.repo.enqueue(tx, case, "extract")
            tx.execute("INSERT INTO inquiry_message VALUES(?,?,?,?,?,?,?,?)",
                       (mid, tenant, source_key, case["inquiry_id"], channel, message["external_id"], encode(message), time.time())).close()
            self.repo.event(tx, case, "source", "message_received", {"message_id": mid})
            return case

    def messages(self, tenant, inquiry_id):
        with self.repo.tx() as tx:
            self.repo.get(tenant, inquiry_id, tx)
            rows = tx.all("SELECT * FROM inquiry_message WHERE tenant_id=? AND inquiry_id=? ORDER BY created_at", (tenant, inquiry_id))
            for row in rows:
                row["payload"] = json.loads(row.pop("payload_json"))
            return rows

    def act(self, actor, inquiry_id, action, payload, *, expected_version, idempotency_key):
        require_actor(actor, review=action in {"approve_due_diligence", "approve_quote", "approve_reply", "confirm_acceptance"})
        if not isinstance(payload, dict) or not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise InquiryError("action_payload_invalid", 422)
        if not idempotency_key or len(idempotency_key) > 150:
            raise InquiryError("idempotency_key_required", 422)
        aid = digest([actor.tenant_id, inquiry_id, idempotency_key])
        request_hash = digest([actor.user_id, action, payload, expected_version])
        with self.repo.tx() as tx:
            case = self.repo.get(actor.tenant_id, inquiry_id, tx)
            old = tx.one("SELECT * FROM inquiry_action WHERE action_id=?", (aid,))
            if old:
                if old["request_hash"] != request_hash:
                    raise InquiryError("idempotency_key_reused")
                return json.loads(old["result_json"])
            if case["version"] != expected_version:
                raise InquiryError("inquiry_version_conflict")
            original = case["version"]
            case["version"] += 1
            data = case["data"]
            jobs = []
            if action == "review_rfq":
                self._state(case, "waiting_customer_info", "waiting_rfq_review",
                            "waiting_due_diligence_review")
                rfq = payload.get("rfq")
                if not isinstance(rfq, dict):
                    raise InquiryError("rfq_required", 422)
                customer_type = payload.get("customer_type")
                if customer_type not in {"company", "individual"}:
                    raise InquiryError("customer_type_required", 422)
                old_rfq = data.get("rfq", {}) if isinstance(data.get("rfq"), dict) else {}
                old_customer = old_rfq.get("customer", {}) if isinstance(old_rfq.get("customer"), dict) else {}
                new_customer = rfq.get("customer", {}) if isinstance(rfq.get("customer"), dict) else {}
                identity_fields = ("company", "website", "email", "address", "city", "phone",
                                   "registration_number", "registration_id")
                old_identity_hash = digest([[old_customer.get(key) for key in identity_fields], old_customer.get("country")])
                new_identity_hash = digest([[new_customer.get(key) for key in identity_fields], new_customer.get("country")])
                if (data.get("research_status") == "identity_rejected"
                        and old_identity_hash == new_identity_hash):
                    raise InquiryError("company_identity_information_unchanged", 422)
                if old_identity_hash != new_identity_hash and data.get("research_status") in {
                        "queued", "failed", "complete", "identity_rejected"}:
                    data["research_revision"] = int(data.get("research_revision", 1)) + 1
                    data["research_status"] = None
                    for key in ("report_id", "research_plan_id", "research_execution_id",
                                "research_completed_revision", "company_identity", "identity_gaps",
                                "due_diligence_approval"):
                        data.pop(key, None)
                data.update(rfq=rfq, customer_type=customer_type, gaps=rfq_gaps(rfq),
                            quote_entry_gaps=quote_entry_gaps(rfq))
                if customer_type == "company" and not rfq.get("customer", {}).get("company"):
                    data["gaps"].append("customer.company")
                profile_row = tx.one("SELECT profile_json FROM inquiry_customer WHERE tenant_id=? AND customer_id=?", (actor.tenant_id, case["customer_id"]))
                profile = json.loads(profile_row["profile_json"])
                profile.update(type=customer_type, customer_provided=rfq.get("customer", {}))
                tx.execute("UPDATE inquiry_customer SET profile_json=? WHERE tenant_id=? AND customer_id=?",
                           (encode(profile), actor.tenant_id, case["customer_id"])).close()
                identity_ready = bool(rfq.get("customer", {}).get("company") or rfq.get("customer", {}).get("website"))
                if customer_type == "company" and not identity_ready:
                    case["state"] = "waiting_customer_info"
                elif data["quote_entry_gaps"]:
                    case["state"] = "waiting_customer_info"
                    # Company research can progress while transaction fields are
                    # being completed, but quote preparation remains blocked.
                    if customer_type == "company" and identity_ready and data.get("research_status") not in {"queued", "complete"}:
                        data["research_status"] = "queued"
                        jobs.append("research")
                elif (data.get("report_id") and data.get("research_status") == "complete"
                      and data.get("research_completed_revision") == data.get("research_revision")):
                    if data.get("due_diligence_approval", {}).get("report_id") == data["report_id"]:
                        case["state"] = "preparing_quote"
                        jobs.append("prepare_quote")
                    else:
                        case["state"] = "waiting_due_diligence_review"
                elif data.get("research_status") == "queued":
                    case["state"] = "researching"
                else:
                    case["state"] = "researching"
                    data["research_status"] = "queued"
                    jobs.append("research")
            elif action == "request_clarification":
                self._state(case, "waiting_customer_info", "waiting_due_diligence_review")
                recipient = data.get("recipient", "")
                body = str(payload.get("body", "")).strip()
                if not EMAIL.fullmatch(recipient) or not body or len(body) > 10000:
                    raise InquiryError("clarification_email_and_body_required", 422)
                clarification = {"recipient": recipient, "account_id": data["account_id"],
                    "subject": ("请补充询盘信息 / " if data.get("reply_language") == "zh"
                                else "Additional information required / ") + inquiry_id, "body": body,
                    "smtp_message_id": f"<{new_id('clarification')}@nanoclaw.local>",
                    "approved_by": actor.user_id, "approved_at": time.time()}
                data["clarification_id"] = self.repo.save_artifact(tx, case, "clarification", clarification)
                data["dispatch_kind"] = "clarify"
                case["state"] = "clarify_queued"
                jobs.append("clarify")
            elif action == "approve_due_diligence":
                self._state(case, "waiting_due_diligence_review")
                report = self.repo.artifact(actor.tenant_id, data["report_id"], tx)
                if report["payload"].get("company_identity", {}).get("status") == "rejected":
                    raise InquiryError("company_identity_rejected", 422)
                if report["payload"].get("gaps") and not str(payload.get("reason", "")).strip():
                    raise InquiryError("unverified_items_require_reason", 422)
                data["due_diligence_approval"] = {"report_id": data["report_id"], "actor": actor.user_id,
                                                 "reason": str(payload.get("reason", "")), "at": time.time()}
                profile_row = tx.one("SELECT profile_json FROM inquiry_customer WHERE tenant_id=? AND customer_id=?", (actor.tenant_id, case["customer_id"]))
                profile = json.loads(profile_row["profile_json"])
                profile["latest_reviewed_report"] = dict(data["due_diligence_approval"])
                tx.execute("UPDATE inquiry_customer SET profile_json=? WHERE tenant_id=? AND customer_id=?",
                           (encode(profile), actor.tenant_id, case["customer_id"])).close()
                data["quote_entry_gaps"] = quote_entry_gaps(data.get("rfq") or {})
                if data["quote_entry_gaps"]:
                    case["state"] = "waiting_customer_info"
                else:
                    case["state"] = "preparing_quote"
                    jobs.append("prepare_quote")
            elif action == "research_more":
                self._state(case, "waiting_due_diligence_review")
                if not str(payload.get("question", "")).strip():
                    raise InquiryError("research_question_required", 422)
                data["research_question"] = str(payload["question"])[:4000]
                data["research_revision"] += 1
                data["research_status"] = "queued"
                case["state"] = "researching"
                jobs.append("research")
            elif action == "save_demo_fallback_note":
                self._state(case, "waiting_customer_info", "waiting_rfq_review",
                            "waiting_due_diligence_review", "waiting_negotiation",
                            "waiting_quote_approval", "waiting_customer_feedback",
                            "retry_pending", "delivery_unknown")
                if not InquirySettings.from_env().demo_mode or data.get("execution_mode") != "demo":
                    raise InquiryError("demo_fallback_only", 403)
                category = str(payload.get("category", "")).strip()
                body = str(payload.get("body", "")).strip()
                if category not in {"official_website", "map_lookup", "customs_lookup",
                                    "news_search", "report_generation", "product_catalog",
                                    "email_delivery", "other"} or not body or len(body) > 4000:
                    raise InquiryError("demo_fallback_note_invalid", 422)
                data["last_demo_fallback_note_id"] = self.repo.save_artifact(tx, case,
                    "demo_fallback_note", {"category": category, "body": body,
                    "demo_data": True, "evidence_status": "not_evidence",
                    "created_by": actor.user_id, "created_at": time.time()})
            elif action == "submit_terms":
                self._state(case, "waiting_negotiation", "waiting_quote_approval")
                terms = deepcopy(payload.get("terms"))
                if not isinstance(terms, dict) or not EMAIL.fullmatch(str(terms.get("recipient", ""))):
                    raise InquiryError("quote_terms_and_recipient_required", 422)
                if not data.get("due_diligence_approval"):
                    raise InquiryError("due_diligence_approval_required")
                required_terms = ("currency", "payment_terms", "trade_term", "delivery", "valid_until")
                if any(not str(terms.get(key) or "").strip() for key in required_terms):
                    raise InquiryError("quote_terms_incomplete", 422)
                lines = terms.get("items")
                if (not isinstance(lines, list) or not lines
                        or any(not isinstance(line, dict)
                               or any(not str(line.get(key) or "").strip()
                                      for key in ("sku", "quantity", "unit", "unit_price"))
                               for line in lines)):
                    raise InquiryError("quote_items_incomplete", 422)
                fees = terms.get("fees")
                if (not isinstance(fees, dict)
                        or set(fees) != {"freight", "packaging", "tax", "other"}
                        or any(str(value).strip() == "" for value in fees.values())):
                    raise InquiryError("quote_fees_incomplete", 422)
                data["terms"] = terms
                data["terms_confirmed_by"] = actor.user_id
                data.pop("sales_handoff_reason", None)
                data.pop("quote_approval", None)
                data.pop("reply_approval", None)
                data.pop("quote_calculation_id", None)
                data.pop("quote_id", None)
                data.pop("reply_draft_id", None)
                case["state"] = "calculating"
                jobs.append("calculate")
            elif action == "edit_reply_draft":
                self._state(case, "waiting_quote_approval")
                original_draft = self.repo.artifact(actor.tenant_id, data.get("quote_id", ""), tx)
                subject = " ".join(str(payload.get("subject", "")).splitlines()).strip()
                body = str(payload.get("body", "")).strip()
                if not subject or not body or len(subject) > 300 or len(body) > 30000:
                    raise InquiryError("reply_draft_subject_and_body_required", 422)
                updated = dict(original_draft["payload"], subject=subject, body=body,
                               edited_by=actor.user_id, edited_at=time.time(),
                               edited_from=original_draft["artifact_id"])
                data["quote_id"] = self.repo.save_artifact(tx, case, "quote", updated)
                data["reply_draft_id"] = data["quote_id"]
                data.pop("quote_approval", None)
                data.pop("reply_approval", None)
                self.repo.event(tx, case, actor.user_id, "reply_draft_edited",
                                {"previous_draft_id": original_draft["artifact_id"]})
            elif action in {"approve_quote", "approve_reply"}:
                self._state(case, "waiting_quote_approval")
                quote = self.repo.artifact(actor.tenant_id, data["quote_id"], tx)
                reply_agent = quote["payload"].get("reply_agent") or {}
                strategy = reply_agent.get("strategy") or next((
                    row.get("strategy") for row in reply_agent.get("tool_trace", [])
                    if row.get("tool") == "choose_reply_strategy"), None)
                if strategy != "compose_quote_reply":
                    raise InquiryError("reply_strategy_requires_resolution", 422)
                if datetime.fromisoformat(quote["payload"]["valid_until"].replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                    raise InquiryError("quote_expired")
                approval = {"quote_id": quote["artifact_id"], "hash": quote["content_hash"],
                            "actor": actor.user_id, "at": time.time()}
                data["quote_approval"] = approval
                data["reply_approval"] = dict(approval)
                case["state"] = "send_queued"
                data["dispatch_kind"] = "send"
                jobs.append("send")
            elif action == "schedule_follow_up":
                self._state(case, "waiting_customer_feedback")
                owner = str(payload.get("owner") or "").strip()
                note = str(payload.get("note") or "").strip()
                due_at = str(payload.get("due_at") or "").strip()
                try:
                    due = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
                    if due.tzinfo is None or due <= datetime.now(timezone.utc):
                        raise ValueError()
                except ValueError:
                    raise InquiryError("follow_up_due_at_invalid", 422)
                if not owner or len(owner) > 120 or len(note) > 4000:
                    raise InquiryError("follow_up_owner_and_note_invalid", 422)
                existing = data.get("follow_up") if isinstance(data.get("follow_up"), dict) else {}
                follow_up = dict(existing, status="scheduled", due_at=due.isoformat(), owner=owner,
                                 note=note, updated_by=actor.user_id, updated_at=time.time())
                data["follow_up"] = follow_up
                data["next_follow_up_at"] = follow_up["due_at"]
                data["follow_up_schedule_id"] = self.repo.save_artifact(
                    tx, case, "follow_up_schedule", follow_up)
            elif action == "prepare_follow_up":
                self._state(case, "waiting_customer_feedback")
                source = self.repo.artifact(actor.tenant_id, data.get("sent_quote_id", ""), tx)
                if source["kind"] != "quote" or source["inquiry_id"] != inquiry_id:
                    raise InquiryError("sent_quote_required", 422)
                quote = source["payload"]
                try:
                    if datetime.fromisoformat(str(quote["valid_until"]).replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                        raise InquiryError("follow_up_quote_expired", 422)
                except (KeyError, ValueError, TypeError):
                    raise InquiryError("follow_up_quote_invalid", 422)
                instruction = str(payload.get("instruction") or "").strip()
                if len(instruction) > 2000:
                    raise InquiryError("follow_up_instruction_too_long", 422)
                language = quote.get("reply_language") or data.get("reply_language") or "en"
                customer = data.get("rfq", {}).get("customer", {})
                name = str(customer.get("name") or ("客户" if language == "zh" else "Customer")).strip()[:120]
                if language == "zh":
                    body = (f"{name}，您好：\n\n想跟进此前发送的报价，确认您是否已经收到，"
                            "以及产品数量、交期、付款或运输条件是否需要调整。")
                    if instruction:
                        body += f"\n\n本次跟进重点：{instruction}"
                    body += "\n\n如方便，请直接回复此邮件，我们会继续协助。\n\n此致\n销售团队"
                else:
                    body = (f"Dear {name},\n\nI am following up on the quotation we sent earlier to confirm that "
                            "you received it and whether the quantity, delivery, payment, or shipping terms need adjustment.")
                    if instruction:
                        body += f"\n\nFollow-up focus: {instruction}"
                    body += "\n\nPlease reply when convenient and we will be glad to assist.\n\nBest regards,\nSales Team"
                sequence = int(data.get("follow_up_count") or 0) + 1
                reply_agent = dict(quote.get("reply_agent") or {})
                reply_agent.update(strategy="compose_quote_reply", reply_language=language,
                                   requires_human_approval=True,
                                   plan_id=f"follow_up_{digest([inquiry_id, source['artifact_id'], sequence])[:24]}")
                trace = list(reply_agent.get("tool_trace") or [])
                trace.append({"tool": "follow_up_email_builder", "status": "completed",
                              "source": "deterministic", "sequence": sequence})
                reply_agent["tool_trace"] = trace
                draft = {key: value for key, value in quote.items()
                         if key not in {"customer_id", "inquiry_id", "version"}}
                draft.update(subject=quote.get("subject") or f"Re: {inquiry_id}", body=body,
                             smtp_message_id=f"<{new_id('followup')}@nanoclaw.local>",
                             in_reply_to=quote.get("smtp_message_id", ""),
                             follow_up_of=source["artifact_id"], follow_up_sequence=sequence,
                             follow_up_instruction=instruction, reply_agent=reply_agent,
                             drafted_by=actor.user_id, drafted_at=time.time())
                data["quote_id"] = self.repo.save_artifact(tx, case, "quote", draft)
                data["reply_draft_id"] = data["quote_id"]
                data.pop("quote_approval", None)
                data.pop("reply_approval", None)
                existing = data.get("follow_up") if isinstance(data.get("follow_up"), dict) else {}
                data["follow_up"] = dict(existing, status="draft_ready", sequence=sequence,
                                         drafted_at=time.time())
                case["state"] = "waiting_quote_approval"
            elif action == "classify_message":
                self._state(case, "needs_message_review")
                if data.get("dispatch_started"):
                    raise InquiryError("delivery_must_finish_or_be_reconciled")
                intent = payload.get("intent")
                if intent in {"new_inquiry", "supplement", "change", "negotiate"}:
                    data.pop("due_diligence_approval", None)
                    data.pop("quote_approval", None)
                    data.pop("reply_approval", None)
                    data["research_revision"] += 1
                    data.pop("research_status", None)
                    data.pop("research_completed_revision", None)
                    case["state"] = "extracting"
                    jobs.append("extract")
                elif intent == "acceptance":
                    if not data.get("sent_quote_id"):
                        raise InquiryError("sent_quote_required")
                    case["state"] = "waiting_acceptance_review"
                elif intent == "reject":
                    case["state"] = "closed"
                elif intent == "unrelated":
                    data["excluded_message_ids"] = list(dict.fromkeys(data.get("excluded_message_ids", []) + data.get("pending_message_ids", [])))
                    case["state"] = data.get("previous_state", "closed")
                    # Restart a superseded read/compute job; never automatically resend.
                    kind = {"extracting": "extract", "researching": "research", "preparing_quote": "prepare_quote",
                            "calculating": "calculate", "composing_reply": "compose_reply"}.get(case["state"])
                    if kind:
                        jobs.append(kind)
                    if case["state"] in {"send_queued", "clarify_queued"}:
                        case["state"] = "waiting_customer_info" if data.get("dispatch_kind") == "clarify" else "waiting_quote_approval"
                    if case["state"] == "sending":
                        case["state"] = "delivery_unknown"
                else:
                    raise InquiryError("message_intent_invalid", 422)
                data["pending_message_ids"] = []
            elif action == "confirm_acceptance":
                self._state(case, "waiting_acceptance_review")
                quote_id = payload.get("quote_id")
                if quote_id != data.get("sent_quote_id"):
                    raise InquiryError("acceptance_quote_mismatch")
                evidence_ids = payload.get("message_ids")
                if not isinstance(evidence_ids, list) or not evidence_ids:
                    raise InquiryError("acceptance_evidence_required", 422)
                quote = self.repo.artifact(actor.tenant_id, quote_id, tx)
                for mid in evidence_ids:
                    if not isinstance(mid, str):
                        raise InquiryError("acceptance_evidence_invalid", 422)
                    source = tx.one("SELECT * FROM inquiry_message WHERE message_id=? AND inquiry_id=? AND tenant_id=?",
                                    (mid, inquiry_id, actor.tenant_id))
                    if not source or source["created_at"] < data.get("sent_at", 0):
                        raise InquiryError("acceptance_evidence_invalid", 422)
                    message = json.loads(source["payload_json"])
                    if message["channel"] == "email" and message["sender"].casefold() != quote["payload"]["recipient"].casefold():
                        raise InquiryError("acceptance_sender_mismatch", 422)
                    if message["channel"] == "whatsapp":
                        if not str(payload.get("note", "")).strip():
                            raise InquiryError("imported_acceptance_requires_identity_confirmation", 422)
                        try:
                            sent = datetime.fromisoformat(message["sent_at"].replace("Z", "+00:00"))
                            if sent.tzinfo is None or sent.timestamp() < data.get("sent_at", 0):
                                raise ValueError()
                        except (KeyError, ValueError, TypeError):
                            raise InquiryError("acceptance_evidence_invalid", 422)
                acceptance = {"quote_id": quote_id, "quote_hash": quote["content_hash"],
                              "agreed_quote": quote["payload"], "source_message_ids": evidence_ids,
                              "confirmed_by": actor.user_id, "confirmed_at": time.time(),
                              "note": str(payload.get("note", ""))[:4000]}
                data["acceptance_id"] = self.repo.save_artifact(tx, case, "acceptance", acceptance)
                case["state"] = "accepted"
            elif action == "retry":
                self._state(case, "retry_pending")
                kind = data.get("failed_job")
                if kind not in {"extract", "research", "prepare_quote", "calculate", "compose_reply", "send", "clarify"}:
                    raise InquiryError("job_not_safely_retryable")
                case["state"] = {"extract": "extracting", "research": "researching", "prepare_quote": "preparing_quote",
                                 "calculate": "calculating", "compose_reply": "composing_reply",
                                 "send": "send_queued", "clarify": "clarify_queued"}[kind]
                jobs.append(kind)
            elif action == "resolve_delivery":
                require_actor(actor, review=True)
                self._state(case, "delivery_unknown")
                if not str(payload.get("reason", "")).strip():
                    raise InquiryError("delivery_reconciliation_reason_required", 422)
                if payload.get("outcome") == "sent":
                    data["dispatch_started"] = False
                    if data.get("dispatch_kind") == "clarify":
                        case["state"] = "waiting_customer_info"
                    else:
                        sent_at = time.time()
                        quote = self.repo.artifact(actor.tenant_id, data["quote_id"], tx)
                        data.update(sent_quote_id=data["quote_id"], sent_at=sent_at)
                        follow_up_after_send(data, dict(quote["payload"], artifact_id=quote["artifact_id"]), sent_at)
                        case["state"] = "waiting_customer_feedback"
                elif payload.get("outcome") == "not_sent":
                    data["dispatch_started"] = False
                    case["state"] = "waiting_customer_info" if data.get("dispatch_kind") == "clarify" else "waiting_quote_approval"
                else:
                    raise InquiryError("delivery_outcome_invalid", 422)
                self.repo.save_artifact(tx, case, "delivery_reconciliation", {
                    "outcome": payload["outcome"], "reason": str(payload["reason"]), "actor": actor.user_id,
                    "outbound_archive": data.get("outbound_archive"), "dispatch_kind": data.get("dispatch_kind")})
            elif action == "close":
                if case["state"] in {"accepted", "closed", "sending"} or data.get("dispatch_started"):
                    raise InquiryError("inquiry_not_closable")
                if not str(payload.get("reason", "")).strip():
                    raise InquiryError("close_reason_required", 422)
                case["state"] = "closed"
                data["close_reason"] = payload["reason"]
            else:
                raise InquiryError("inquiry_action_unknown", 422)
            self.repo.write_case(tx, case, original)
            for kind in jobs:
                self.repo.enqueue(tx, case, kind)
            self.repo.event(tx, case, actor.user_id, action, {"reason": payload.get("reason", "")})
            tx.execute("INSERT INTO inquiry_action VALUES(?,?,?,?)", (aid, inquiry_id, request_hash, encode(case))).close()
            return case

    @staticmethod
    def _state(case, *allowed):
        if case["state"] not in allowed:
            raise InquiryError("inquiry_transition_not_allowed")
