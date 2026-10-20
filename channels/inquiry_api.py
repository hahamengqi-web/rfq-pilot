"""Internal inquiry UI/API. Principals are resolved by the host, never by an LLM."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from agent.inquiry.config import InquirySettings
from agent.inquiry.repository import InquiryError
from agent.inquiry.runtime import default_service, ingest_envelope
from agent.inquiry.service import require_actor


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(max_length=60)
    expected_version: StrictInt = Field(ge=1)
    payload: dict = Field(default_factory=dict)


class BulkDeleteItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inquiry_id: str = Field(pattern=r"^INQ_[0-9a-f]{32}$")
    expected_version: StrictInt = Field(ge=1)


class BulkDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[BulkDeleteItem] = Field(min_length=1, max_length=100)


def create_inquiry_router(actor_for_request, service_factory=default_service):
    router = APIRouter(prefix="/api/inquiries", tags=["inquiries"])

    def actor(request):
        principal = actor_for_request(request)
        try:
            require_actor(principal)
        except InquiryError as exc:
            raise HTTPException(exc.status, exc.code) from exc
        return principal

    @router.get("")
    async def cases(request: Request):
        principal = actor(request)
        return {"items": service_factory().repo.list(principal.tenant_id)}

    @router.post("/bulk-delete")
    async def bulk_delete(payload: BulkDelete, request: Request):
        principal = actor(request)
        try:
            return await asyncio.to_thread(
                service_factory().delete_cases, principal,
                [item.model_dump() for item in payload.items],
            )
        except InquiryError as exc:
            raise HTTPException(exc.status, exc.code) from exc

    @router.post("/demo-case")
    async def create_demo_case(request: Request):
        principal = actor(request)
        settings = InquirySettings.from_env()
        if not (settings.demo_mode and settings.demo_prefer_fixtures) or settings.smtp_enabled:
            raise HTTPException(403, "fixed_demo_case_unavailable")
        sample = Path(__file__).resolve().parents[1] / "examples" / "demo" / "northstar-inquiry.eml"
        try:
            raw = sample.read_bytes()
        except OSError as exc:
            raise HTTPException(503, "demo_email_fixture_missing") from exc
        original_id = b"<demo-rfq-001@northstar-retail.example>"
        if original_id not in raw:
            raise HTTPException(503, "demo_email_fixture_invalid")
        unique_id = f"<demo-rfq-{uuid.uuid4().hex}@northstar-retail.example>".encode("ascii")
        raw = raw.replace(original_id, unique_id, 1)
        from channels.email.mime_parser import parse_email
        try:
            envelope = parse_email(raw, account_id="demo-fixture", provider="import",
                                   folder="demo", uidvalidity=1, uid=1)
            result = await asyncio.to_thread(
                ingest_envelope, envelope, service=service_factory(),
                settings=replace(settings, tenant_id=principal.tenant_id), demo_fixture=True,
            )
            return {"inquiry_id": result["inquiry_id"], "state": result["state"], "demo_fixture": True}
        except InquiryError as exc:
            raise HTTPException(exc.status, exc.code) from exc
        except ValueError as exc:
            raise HTTPException(422, "demo_email_fixture_invalid") from exc

    @router.get("/customers/{customer_id}")
    async def customer(customer_id: str, request: Request):
        principal = actor(request)
        with service_factory().repo.tx() as tx:
            row = tx.one("SELECT * FROM inquiry_customer WHERE tenant_id=? AND customer_id=?", (principal.tenant_id, customer_id))
            if not row:
                raise HTTPException(404, "customer_not_found")
            row["profile"] = json.loads(row.pop("profile_json"))
            records = tx.all("SELECT artifact_id,inquiry_id,kind,version,content_hash,payload_json,created_at FROM inquiry_artifact WHERE tenant_id=? AND customer_id=? AND kind IN ('report','acceptance') ORDER BY created_at DESC", (principal.tenant_id, customer_id))
            for record in records:
                record["payload"] = json.loads(record.pop("payload_json"))
            return {"customer": row, "records": records}

    @router.post("/import-email")
    async def import_email(request: Request, account_id: str):
        principal = actor(request)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 30 * 1024 * 1024:
                raise HTTPException(413, "email_too_large")
        from channels.email.mime_parser import parse_email
        try:
            envelope = parse_email(bytes(raw), account_id=account_id, provider="import",
                                   folder="import", uidvalidity=1, uid=1)
            # Content hash prevents imports without Message-ID from colliding.
            if not envelope.internet_message_id:
                envelope = replace(envelope, provider_message_id="import:" + envelope.raw_sha256)
            return await asyncio.to_thread(ingest_envelope, envelope, service=service_factory(),
                                           settings=replace(InquirySettings.from_env(), tenant_id=principal.tenant_id))
        except InquiryError as exc:
            raise HTTPException(exc.status, exc.code) from exc
        except ValueError as exc:
            raise HTTPException(422, "email_parse_invalid") from exc

    @router.post("/import-whatsapp")
    async def import_whatsapp(request: Request):
        principal = actor(request)
        body = await request.body()
        if len(body) > 1024 * 1024:
            raise HTTPException(413, "import_too_large")
        try:
            payload = json.loads(body)
            messages = payload["messages"]
            if not isinstance(messages, list) or not 1 <= len(messages) <= 200:
                raise ValueError()
            results = []
            inquiry_id = payload.get("inquiry_id")
            for row in messages:
                if not all(isinstance(row.get(k), str) and row[k] for k in ("id", "sender", "text", "sent_at")):
                    raise ValueError()
                result = service_factory().ingest(principal.tenant_id, {
                    "channel": "whatsapp", "external_id": row["id"], "sender": row["sender"],
                    "sender_name": row.get("name", ""), "text": row["text"], "sent_at": row["sent_at"],
                    "account_id": str(payload.get("account_id", "manual-import")),
                    "imported_by": principal.user_id, "identity_status": "unverified", "references": [],
                    "attachments": row.get("attachments", []),
                    "execution_mode": "demo" if InquirySettings.from_env().demo_mode else "production",
                }, inquiry_id=inquiry_id)
                inquiry_id = result["inquiry_id"]
                results.append(result)
            return {"items": results, "inquiry_id": inquiry_id}
        except InquiryError as exc:
            raise HTTPException(exc.status, exc.code) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(422, "whatsapp_import_invalid") from exc

    @router.get("/{inquiry_id}")
    async def detail(inquiry_id: str, request: Request):
        principal = actor(request)
        service = service_factory()
        try:
            case = service.repo.get(principal.tenant_id, inquiry_id)
            with service.repo.tx() as tx:
                events = tx.all("SELECT event_type,actor,version,detail_json,created_at FROM inquiry_event WHERE tenant_id=? AND inquiry_id=? ORDER BY created_at", (principal.tenant_id, inquiry_id))
            return {"case": case, "artifacts": service.repo.artifacts(principal.tenant_id, inquiry_id),
                    "messages": service.messages(principal.tenant_id, inquiry_id), "events": events}
        except InquiryError as exc:
            raise HTTPException(exc.status, exc.code) from exc

    @router.get("/{inquiry_id}/quotation")
    async def quotation(inquiry_id: str, request: Request):
        principal = actor(request)
        try:
            service = service_factory()
            case = service.repo.get(principal.tenant_id, inquiry_id)
            with service.repo.tx() as tx:
                artifact = service.repo.artifact(principal.tenant_id, case["data"].get("quote_id", ""), tx)
            return Response(artifact["payload"]["document_html"], media_type="text/html",
                            headers={"Content-Disposition": f'attachment; filename="{inquiry_id}-quotation.html"',
                                     "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})
        except InquiryError as exc:
            raise HTTPException(exc.status, exc.code) from exc

    @router.post("/{inquiry_id}/actions")
    async def act(inquiry_id: str, payload: Action, request: Request):
        principal = actor(request)
        try:
            return service_factory().act(principal, inquiry_id, payload.action, payload.payload,
                                         expected_version=payload.expected_version,
                                         idempotency_key=request.headers.get("Idempotency-Key", ""))
        except InquiryError as exc:
            raise HTTPException(exc.status, exc.code) from exc

    return router
