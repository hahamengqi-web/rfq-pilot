"""Production ingestion uses the same selected business DB as the workflow."""
from __future__ import annotations

import asyncio
import time

from .config import InquirySettings
from .runtime import default_service, ingest_envelope
from .storage import LocalDemoEmailArchive, S3EmailArchive


class InquiryMailboxPoller:
    def __init__(self, *, service=None, settings=None, accounts=None, secrets=None, source_factory=None, archive=None):
        from agent.business.email_account_service import create_default_email_account_service
        from agent.business.email_secret_store import create_default_email_secret_store
        from channels.email.imap_source import ImapEmailSource
        self.service = service or default_service()
        self.settings = settings or InquirySettings.from_env()
        self.accounts = accounts or create_default_email_account_service().repository
        self.secrets = secrets or create_default_email_secret_store()
        self.source_factory = source_factory or ImapEmailSource
        self.archive = archive or (
            LocalDemoEmailArchive() if self.settings.demo_mode else S3EmailArchive(self.settings)
        )
        self.next_due = {}

    async def poll_due(self):
        from channels.email.imap_source import ImapSettings
        from agent.business.email_config import load_email_config
        results = []
        for account in self.accounts.list_active():
            if not account["inbound_enabled"] or account["status"] not in {"healthy", "degraded", "validating"}:
                continue
            aid = account["account_id"]
            if time.monotonic() < self.next_due.get(aid, 0):
                continue
            self.next_due[aid] = time.monotonic() + max(30, int(account["poll_seconds"]))
            try:
                with self.service.repo.tx() as tx:
                    cursor = tx.one("SELECT * FROM inquiry_mail_cursor WHERE tenant_id=? AND account_id=? AND folder=?",
                                    (self.settings.tenant_id, aid, account["folder"]))
                settings = ImapSettings(provider=account["provider"], account_id=aid, address=account["address"],
                    auth_code=self.secrets.get(account["secret_ref"]), folder=account["folder"],
                    prefer_recent_on_first_poll=self.settings.demo_mode)
                source = self.source_factory(settings, load_email_config().limits())
                source.expected_uidvalidity = cursor["uidvalidity"] if cursor else 0
                envelopes = await asyncio.to_thread(source.fetch_after, cursor["last_uid"] if cursor else 0, 30)
                messages = []
                for envelope in envelopes:
                    result = await asyncio.to_thread(ingest_envelope, envelope, service=self.service,
                        archive=self.archive, settings=self.settings, mailbox_address=account["address"],
                        allowed_senders=tuple(account.get("allowed_senders", [])))
                    with self.service.repo.tx() as tx:
                        old = tx.one("SELECT * FROM inquiry_mail_cursor WHERE tenant_id=? AND account_id=? AND folder=?",
                                     (self.settings.tenant_id, aid, account["folder"]))
                        if old:
                            last_uid = max(old["last_uid"], envelope.uid) if old["uidvalidity"] == envelope.uidvalidity else envelope.uid
                            tx.execute("UPDATE inquiry_mail_cursor SET uidvalidity=?,last_uid=? WHERE tenant_id=? AND account_id=? AND folder=?",
                                       (envelope.uidvalidity, last_uid, self.settings.tenant_id, aid, account["folder"])).close()
                        else:
                            tx.execute("INSERT INTO inquiry_mail_cursor VALUES(?,?,?,?,?)",
                                       (aid, account["folder"], self.settings.tenant_id, envelope.uidvalidity, envelope.uid)).close()
                    messages.append(result)
                results.append({"account_id": aid, "status": "polled", "messages": messages})
            except Exception as exc:
                results.append({"account_id": aid, "status": "error", "error_code": getattr(exc, "code", type(exc).__name__)})
        return results
