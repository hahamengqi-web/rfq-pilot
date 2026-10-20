"""SQLite/MySQL business authority, optimistic versions and durable job outbox.

Connections are scoped to transactions; no network/model work is done inside them.
JSON documents hold versioned structured contracts, not serialized Agent sessions.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from agent.business.config import load_business_config


class InquiryError(ValueError):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def new_id(prefix):
    return prefix + "_" + uuid.uuid4().hex


def research_run_id(tenant, inquiry_id, revision):
    return "research_" + digest([tenant, inquiry_id, int(revision)])[:32]


class Transaction:
    def __init__(self, connection, mysql):
        self.connection, self.mysql = connection, mysql

    def execute(self, sql, args=()):
        cursor = self.connection.cursor()
        cursor.execute(sql.replace("?", "%s") if self.mysql else sql, args)
        return cursor

    def one(self, sql, args=()):
        cursor = self.execute(sql, args)
        try:
            row = cursor.fetchone()
            return dict(row) if row is not None else None
        finally:
            cursor.close()

    def all(self, sql, args=()):
        cursor = self.execute(sql, args)
        try:
            return [dict(row) for row in cursor.fetchall()]
        finally:
            cursor.close()


class InquiryRepository:
    def __init__(self, *, sqlite_path=None, config=None):
        self.config = config or load_business_config()
        self.mysql = sqlite_path is None and self.config.database_backend == "mysql"
        self.path = str(sqlite_path or self.config.database_path)
        if not self.mysql:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def connect(self):
        if self.mysql:
            import pymysql
            return pymysql.connect(host=self.config.mysql_host, port=self.config.mysql_port,
                                   user=self.config.mysql_user, password=self.config.mysql_password,
                                   database=self.config.mysql_database, charset="utf8mb4",
                                   cursorclass=pymysql.cursors.DictCursor, autocommit=False,
                                   connect_timeout=5)
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def tx(self):
        conn = self.connect()
        try:
            if self.mysql:
                conn.begin()
            else:
                conn.execute("BEGIN IMMEDIATE")
            yield Transaction(conn, self.mysql)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self):
        # Separate versioned aggregate from legacy ops tables; legacy IDs are
        # retained in customer profiles and quote references during migration.
        statements = [
            """CREATE TABLE IF NOT EXISTS inquiry_mail_cursor (
                account_id VARCHAR(100) NOT NULL, folder VARCHAR(100) NOT NULL,
                tenant_id VARCHAR(100) NOT NULL, uidvalidity BIGINT NOT NULL,
                last_uid BIGINT NOT NULL, PRIMARY KEY(tenant_id,account_id,folder))""",
            """CREATE TABLE IF NOT EXISTS inquiry_customer (
                customer_id VARCHAR(64) PRIMARY KEY, tenant_id VARCHAR(100) NOT NULL,
                identity_key VARCHAR(64) NOT NULL, profile_json TEXT NOT NULL,
                created_at DOUBLE NOT NULL, UNIQUE(tenant_id,identity_key))""",
            """CREATE TABLE IF NOT EXISTS inquiry_case (
                inquiry_id VARCHAR(64) PRIMARY KEY, tenant_id VARCHAR(100) NOT NULL,
                customer_id VARCHAR(64) NOT NULL, state VARCHAR(64) NOT NULL,
                version INTEGER NOT NULL, data_json TEXT NOT NULL,
                created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS inquiry_message (
                message_id VARCHAR(64) PRIMARY KEY, tenant_id VARCHAR(100) NOT NULL,
                source_key VARCHAR(64) NOT NULL, inquiry_id VARCHAR(64),
                channel VARCHAR(20) NOT NULL, external_id VARCHAR(512) NOT NULL,
                payload_json TEXT NOT NULL, created_at DOUBLE NOT NULL,
                UNIQUE(tenant_id,source_key))""",
            """CREATE TABLE IF NOT EXISTS inquiry_artifact (
                artifact_id VARCHAR(64) PRIMARY KEY, tenant_id VARCHAR(100) NOT NULL,
                inquiry_id VARCHAR(64) NOT NULL, customer_id VARCHAR(64) NOT NULL,
                kind VARCHAR(40) NOT NULL, version INTEGER NOT NULL,
                content_hash VARCHAR(64) NOT NULL, payload_json TEXT NOT NULL,
                created_at DOUBLE NOT NULL, UNIQUE(inquiry_id,kind,version))""",
            """CREATE TABLE IF NOT EXISTS inquiry_action (
                action_id VARCHAR(64) PRIMARY KEY, inquiry_id VARCHAR(64) NOT NULL,
                request_hash VARCHAR(64) NOT NULL, result_json TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS inquiry_job (
                job_id VARCHAR(64) PRIMARY KEY, inquiry_id VARCHAR(64) NOT NULL,
                tenant_id VARCHAR(100) NOT NULL, kind VARCHAR(40) NOT NULL,
                expected_version INTEGER NOT NULL, research_revision INTEGER,
                status VARCHAR(24) NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, lease_until DOUBLE NOT NULL DEFAULT 0,
                lease_token VARCHAR(64), error_code VARCHAR(100), created_at DOUBLE NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS inquiry_event (
                event_id VARCHAR(64) PRIMARY KEY, inquiry_id VARCHAR(64) NOT NULL,
                tenant_id VARCHAR(100) NOT NULL, version INTEGER NOT NULL,
                actor VARCHAR(100) NOT NULL, event_type VARCHAR(64) NOT NULL,
                detail_json TEXT NOT NULL, created_at DOUBLE NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS inquiry_research_run (
                run_id VARCHAR(64) PRIMARY KEY, tenant_id VARCHAR(100) NOT NULL,
                inquiry_id VARCHAR(64) NOT NULL, customer_id VARCHAR(64) NOT NULL,
                research_revision INTEGER NOT NULL, status VARCHAR(24) NOT NULL,
                phase VARCHAR(40) NOT NULL, checkpoint_json TEXT NOT NULL,
                report_model VARCHAR(160), fallback_used INTEGER NOT NULL DEFAULT 0,
                error_code VARCHAR(160), started_at DOUBLE NOT NULL,
                updated_at DOUBLE NOT NULL, finished_at DOUBLE,
                UNIQUE(tenant_id,inquiry_id,research_revision))""",
            """CREATE TABLE IF NOT EXISTS inquiry_research_step (
                step_record_id VARCHAR(64) PRIMARY KEY, run_id VARCHAR(64) NOT NULL,
                step_id VARCHAR(80) NOT NULL, tool VARCHAR(40) NOT NULL,
                query_hash VARCHAR(64) NOT NULL, query_summary TEXT NOT NULL,
                status VARCHAR(32) NOT NULL, reason VARCHAR(500),
                evidence_count INTEGER NOT NULL DEFAULT 0,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                started_at DOUBLE, finished_at DOUBLE, updated_at DOUBLE NOT NULL,
                UNIQUE(run_id,step_id))""",
            """CREATE TABLE IF NOT EXISTS inquiry_research_evidence (
                evidence_row_id VARCHAR(64) PRIMARY KEY, run_id VARCHAR(64) NOT NULL,
                evidence_id VARCHAR(96) NOT NULL, step_id VARCHAR(80),
                company_identity_id VARCHAR(64), source_type VARCHAR(40) NOT NULL,
                quality VARCHAR(40) NOT NULL, url TEXT,
                content_hash VARCHAR(64) NOT NULL, payload_json TEXT NOT NULL,
                collected_at DOUBLE NOT NULL, UNIQUE(run_id,evidence_id))""",
        ]
        with self.tx() as tx:
            for sql in statements:
                # Reports and extracted messages can exceed MySQL's 64 KiB TEXT.
                tx.execute(sql.replace(" TEXT", " LONGTEXT") if self.mysql else sql).close()
            columns = (tx.all("SHOW COLUMNS FROM inquiry_job") if self.mysql
                       else tx.all("PRAGMA table_info(inquiry_job)"))
            names = {row.get("Field") if self.mysql else row.get("name") for row in columns}
            if "research_revision" not in names:
                tx.execute("ALTER TABLE inquiry_job ADD COLUMN research_revision INTEGER").close()

    @staticmethod
    def unpack(row):
        if row is None:
            raise InquiryError("inquiry_not_found", 404)
        row["data"] = json.loads(row.pop("data_json"))
        return row

    def get(self, tenant, inquiry_id, tx=None):
        sql = "SELECT * FROM inquiry_case WHERE tenant_id=? AND inquiry_id=?"
        if tx:
            return self.unpack(tx.one(sql + (" FOR UPDATE" if self.mysql else ""), (tenant, inquiry_id)))
        with self.tx() as unit:
            return self.unpack(unit.one(sql, (tenant, inquiry_id)))

    def list(self, tenant, limit=100):
        with self.tx() as tx:
            return [self.unpack(row) for row in tx.all(
                "SELECT * FROM inquiry_case WHERE tenant_id=? ORDER BY updated_at DESC LIMIT ?",
                (tenant, min(200, max(1, int(limit)))))]

    def artifacts(self, tenant, inquiry_id):
        with self.tx() as tx:
            self.get(tenant, inquiry_id, tx)
            rows = tx.all("SELECT * FROM inquiry_artifact WHERE tenant_id=? AND inquiry_id=? ORDER BY created_at",
                          (tenant, inquiry_id))
            for row in rows:
                row["payload"] = json.loads(row.pop("payload_json"))
            return rows

    def artifact(self, tenant, artifact_id, tx):
        row = tx.one("SELECT * FROM inquiry_artifact WHERE tenant_id=? AND artifact_id=?", (tenant, artifact_id))
        if not row:
            raise InquiryError("artifact_not_found", 404)
        row["payload"] = json.loads(row.pop("payload_json"))
        return row

    def save_artifact(self, tx, case, kind, payload):
        row = tx.one("SELECT MAX(version) AS n FROM inquiry_artifact WHERE inquiry_id=? AND kind=?",
                     (case["inquiry_id"], kind))
        version = int(row["n"] or 0) + 1
        artifact_id = new_id(kind)
        payload = dict(payload, customer_id=case["customer_id"], inquiry_id=case["inquiry_id"], version=version)
        tx.execute("INSERT INTO inquiry_artifact VALUES(?,?,?,?,?,?,?,?,?)",
                   (artifact_id, case["tenant_id"], case["inquiry_id"], case["customer_id"], kind,
                    version, digest(payload), encode(payload), time.time())).close()
        return artifact_id

    def delete_cases(self, tx, tenant, inquiry_ids):
        """Delete selected inquiries while retaining minimal source dedupe markers."""
        for inquiry_id in sorted(inquiry_ids):
            scope = (tenant, inquiry_id)
            case_row = tx.one("SELECT customer_id FROM inquiry_case WHERE tenant_id=? AND inquiry_id=?", scope)
            report_ids = {row["artifact_id"] for row in tx.all(
                "SELECT artifact_id FROM inquiry_artifact WHERE tenant_id=? AND inquiry_id=? AND kind='report'", scope)}
            if case_row and report_ids:
                customer = tx.one("SELECT profile_json FROM inquiry_customer WHERE tenant_id=? AND customer_id=?",
                                  (tenant, case_row["customer_id"]))
                if customer:
                    profile = json.loads(customer["profile_json"])
                    latest = profile.get("latest_reviewed_report")
                    if isinstance(latest, dict) and latest.get("report_id") in report_ids:
                        profile.pop("latest_reviewed_report", None)
                        tx.execute("UPDATE inquiry_customer SET profile_json=? WHERE tenant_id=? AND customer_id=?",
                                   (encode(profile), tenant, case_row["customer_id"])).close()
            run_ids = [row["run_id"] for row in tx.all(
                "SELECT run_id FROM inquiry_research_run WHERE tenant_id=? AND inquiry_id=?", scope)]
            for run_id in run_ids:
                tx.execute("DELETE FROM inquiry_research_evidence WHERE run_id=?", (run_id,)).close()
                tx.execute("DELETE FROM inquiry_research_step WHERE run_id=?", (run_id,)).close()
            tx.execute("DELETE FROM inquiry_research_run WHERE tenant_id=? AND inquiry_id=?", scope).close()
            tx.execute("DELETE FROM inquiry_artifact WHERE tenant_id=? AND inquiry_id=?", scope).close()
            # Keep source keys so mailbox polling does not recreate deleted inquiries.
            tx.execute("UPDATE inquiry_message SET inquiry_id=NULL,payload_json='{}' "
                       "WHERE tenant_id=? AND inquiry_id=?", scope).close()
            tx.execute("DELETE FROM inquiry_job WHERE tenant_id=? AND inquiry_id=?", scope).close()
            tx.execute("DELETE FROM inquiry_event WHERE tenant_id=? AND inquiry_id=?", scope).close()
            tx.execute("DELETE FROM inquiry_action WHERE inquiry_id=?", (inquiry_id,)).close()
            tx.execute("DELETE FROM inquiry_case WHERE tenant_id=? AND inquiry_id=?", scope).close()

    def event(self, tx, case, actor, name, detail=None):
        tx.execute("INSERT INTO inquiry_event VALUES(?,?,?,?,?,?,?,?)",
                   (new_id("event"), case["inquiry_id"], case["tenant_id"], case["version"], actor,
                    name, encode(detail or {}), time.time())).close()

    def enqueue(self, tx, case, kind):
        if kind == "research" and not self.reserve_research(tx, case):
            return False
        tx.execute("INSERT INTO inquiry_job(job_id,inquiry_id,tenant_id,kind,expected_version,research_revision,status,created_at) VALUES(?,?,?,?,?,?,'pending',?)",
                   (new_id("job"), case["inquiry_id"], case["tenant_id"], kind, case["version"],
                    int(case.get("data", {}).get("research_revision", 1)) if kind == "research" else None,
                    time.time())).close()
        return True

    def reserve_research(self, tx, case):
        revision = int(case.get("data", {}).get("research_revision", 1))
        run_id = research_run_id(case["tenant_id"], case["inquiry_id"], revision)
        now = time.time()
        sql = ("INSERT IGNORE INTO inquiry_research_run"
               if tx.mysql else "INSERT OR IGNORE INTO inquiry_research_run")
        cursor = tx.execute(
            sql + "(run_id,tenant_id,inquiry_id,customer_id,research_revision,status,phase,checkpoint_json,started_at,updated_at) VALUES(?,?,?,?,?,'queued','queued','{}',?,?)",
            (run_id, case["tenant_id"], case["inquiry_id"], case["customer_id"], revision, now, now),
        )
        inserted = cursor.rowcount == 1
        cursor.close()
        if not inserted:
            existing = tx.one("SELECT status FROM inquiry_research_run WHERE run_id=?", (run_id,))
            if existing and existing["status"] == "failed":
                tx.execute(
                    "UPDATE inquiry_research_run SET status='queued',phase='queued',error_code=NULL,finished_at=NULL,updated_at=? WHERE run_id=?",
                    (now, run_id),
                ).close()
                return True
        return inserted

    def mark_research_failed(self, tx, case, error_code):
        revision = int(case.get("data", {}).get("research_revision", 1))
        tx.execute(
            "UPDATE inquiry_research_run SET status='failed',phase='failed',error_code=?,updated_at=?,finished_at=? WHERE tenant_id=? AND inquiry_id=? AND research_revision=?",
            (str(error_code)[:160], time.time(), time.time(), case["tenant_id"],
             case["inquiry_id"], revision),
        ).close()

    def write_case(self, tx, case, previous_version):
        cursor = tx.execute("UPDATE inquiry_case SET state=?,version=?,data_json=?,updated_at=? WHERE inquiry_id=? AND tenant_id=? AND version=?",
                            (case["state"], case["version"], encode(case["data"]), time.time(),
                             case["inquiry_id"], case["tenant_id"], previous_version))
        changed = cursor.rowcount
        cursor.close()
        if changed != 1:
            raise InquiryError("inquiry_version_conflict")

    def claim(self, tenant):
        with self.tx() as tx:
            suffix = " FOR UPDATE SKIP LOCKED" if self.mysql else ""
            job = tx.one("SELECT * FROM inquiry_job WHERE tenant_id=? AND (status='pending' OR (status='running' AND lease_until<?)) ORDER BY created_at LIMIT 1" + suffix,
                         (tenant, time.time()))
            if not job:
                return None
            job["recovered"] = job["status"] == "running"
            job["lease_token"] = uuid.uuid4().hex
            tx.execute("UPDATE inquiry_job SET status='running',lease_until=?,lease_token=?,attempts=attempts+1 WHERE job_id=?",
                       (time.time() + 900, job["lease_token"], job["job_id"])).close()
            return job

    def finish(self, tx, job, status="done", error=None):
        cursor = tx.execute("UPDATE inquiry_job SET status=?,error_code=?,lease_until=0 WHERE job_id=? AND lease_token=? AND status='running'",
                            (status, error, job["job_id"], job["lease_token"]))
        changed = cursor.rowcount
        cursor.close()
        if changed != 1:
            raise InquiryError("job_lease_lost")
