"""Copy legacy mailbox account settings to the active MySQL store.

Only account metadata is copied. Authorization codes remain in the existing
DPAPI secret store, and the source SQLite database is never modified.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from agent.business.config import load_business_config
from agent.business.email_secret_store import create_default_email_secret_store
from agent.business.mysql_database import create_connection


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "agent/business/migrations/003_email_accounts.mysql.sql"


def mysql_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def migrate(source: Path) -> dict:
    config = load_business_config()
    if config.database_backend != "mysql":
        raise RuntimeError("BUSINESS_DATABASE_BACKEND=mysql is required")
    legacy = (sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)
              if source.is_file() else None)
    if legacy is not None:
        legacy.row_factory = sqlite3.Row
    target = create_connection()
    secrets = create_default_email_secret_store()
    created = skipped = missing_secrets = 0
    try:
        accounts = (legacy.execute("SELECT * FROM ops_email_account WHERE deleted_at IS NULL").fetchall()
                    if legacy is not None else [])
        with target.cursor() as cursor:
            schema_text = "\n".join(line for line in SCHEMA.read_text(encoding="utf-8").splitlines()
                                    if not line.lstrip().startswith("--"))
            for statement in schema_text.split(";"):
                sql = statement.strip()
                if sql:
                    cursor.execute(sql)
            for account in accounts:
                cursor.execute("SELECT account_id FROM ops_email_account WHERE account_id=%s", (account["account_id"],))
                if cursor.fetchone():
                    skipped += 1
                    continue
                has_secret = secrets.contains(account["secret_ref"])
                if not has_secret:
                    missing_secrets += 1
                cursor.execute("""INSERT INTO ops_email_account (
                    account_id,display_name,provider,address,secret_ref,folder,inbound_enabled,
                    outbound_enabled,poll_seconds,sender_name,allowed_senders_json,
                    allowed_recipients_json,status,last_checked_at,last_error_code,
                    config_version,created_at,updated_at,deleted_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL)""", (
                    account["account_id"], account["display_name"], account["provider"],
                    account["address"], account["secret_ref"], account["folder"],
                    bool(account["inbound_enabled"]), bool(account["outbound_enabled"]),
                    account["poll_seconds"], account["sender_name"],
                    account["allowed_senders_json"], account["allowed_recipients_json"],
                    account["status"] if has_secret else "disabled",
                    mysql_time(account["last_checked_at"]), account["last_error_code"],
                    account["config_version"], mysql_time(account["created_at"]),
                    mysql_time(account["updated_at"]),
                ))
                cursor.execute("""INSERT INTO ops_email_account_audit
                    (account_id,actor,action,changed_fields_json,created_at)
                    VALUES (%s,%s,%s,%s,%s)""", (
                    account["account_id"], "migration", "legacy_sqlite_import",
                    json.dumps(["account_metadata"], ensure_ascii=False),
                    datetime.now(timezone.utc).replace(tzinfo=None),
                ))
                created += 1
        target.commit()
        return {"source_accounts": len(accounts), "created": created, "already_present": skipped,
                "missing_authorization_codes": missing_secrets}
    except Exception:
        target.rollback()
        raise
    finally:
        if legacy is not None:
            legacy.close()
        target.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "agent/business/data/business.db")
    parser.add_argument("--apply", action="store_true", help="create MySQL tables and copy accounts")
    args = parser.parse_args()
    if not args.apply:
        parser.error("--apply is required")
    print(json.dumps(migrate(args.source), ensure_ascii=False))
