from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class InquirySettings:
    enabled: bool = True
    tenant_id: str = "default"
    redis_url: str = "redis://127.0.0.1:6379/3"
    memory_ttl: int = 86400
    research_steps: int = 6
    search_key: str = ""
    search_provider: str = "tavily"
    map_api_key: str = ""
    customs_api_key: str = ""
    customs_base_url: str = ""
    customs_provider: str = ""
    customs_records_per_direction: int = 3
    news_api_key: str = ""
    crawl_max_pages: int = 12
    crawl_max_depth: int = 2
    report_api_key: str = ""
    report_base_url: str = "https://api.deepseek.com"
    report_model: str = "deepseek-v4-pro"
    report_max_tokens: int = 8192
    report_timeout_seconds: int = 180
    report_max_retries: int = 2
    report_repair_attempts: int = 1
    report_reasoning_effort: str = "high"
    report_fallback_model: str = "deepseek-flash"
    report_fallback_max_retries: int = 1
    s3_bucket: str = ""
    s3_endpoint: str | None = None
    s3_region: str = "us-east-1"
    s3_prefix: str = "nanoclaw/email"
    s3_kms_key: str = ""
    smtp_enabled: bool = False
    auto_clarify: bool = False
    demo_mode: bool = False
    demo_prefer_fixtures: bool = False
    demo_live_mailbox: bool = False
    demo_external_timeout_seconds: int = 8
    demo_external_retries: int = 1
    demo_report_timeout_seconds: int = 45

    @classmethod
    def from_env(cls):
        report_reasoning_effort = os.getenv(
            "DUE_DILIGENCE_REPORT_REASONING_EFFORT", "high").strip().lower()
        if report_reasoning_effort not in {"low", "high", "max"}:
            report_reasoning_effort = "high"
        return cls(
            enabled=os.getenv("INQUIRY_WORKFLOW_ENABLED", "true").lower() == "true",
            tenant_id=os.getenv("INQUIRY_TENANT_ID", os.getenv("NANOCLAW_ACCESS_TENANT_ID", "default")),
            redis_url=os.getenv("DUE_DILIGENCE_REDIS_URL", cls.redis_url),
            memory_ttl=max(60, int(os.getenv("DUE_DILIGENCE_MEMORY_TTL_SECONDS", "86400"))),
            research_steps=max(1, min(12, int(os.getenv("DUE_DILIGENCE_MAX_STEPS", "6")))),
            search_key=os.getenv("DUE_DILIGENCE_SEARCH_API_KEY") or os.getenv("TAVILY_API_KEY", ""),
            search_provider=os.getenv("DUE_DILIGENCE_SEARCH_PROVIDER", "tavily").strip().lower(),
            map_api_key=os.getenv("DUE_DILIGENCE_MAPS_API_KEY", ""),
            customs_api_key=os.getenv("DUE_DILIGENCE_CUSTOMS_API_KEY", ""),
            customs_base_url=os.getenv("DUE_DILIGENCE_CUSTOMS_BASE_URL", ""),
            customs_provider=os.getenv("DUE_DILIGENCE_CUSTOMS_PROVIDER", "").strip().lower(),
            customs_records_per_direction=max(1, min(10, int(os.getenv(
                "DUE_DILIGENCE_CUSTOMS_RECORDS_PER_DIRECTION", "3")))),
            news_api_key=os.getenv("DUE_DILIGENCE_NEWS_API_KEY", ""),
            crawl_max_pages=max(1, min(30, int(os.getenv("DUE_DILIGENCE_CRAWL_MAX_PAGES", "12")))),
            crawl_max_depth=max(0, min(3, int(os.getenv("DUE_DILIGENCE_CRAWL_MAX_DEPTH", "2")))),
            report_api_key=os.getenv("DUE_DILIGENCE_REPORT_API_KEY", ""),
            report_base_url=os.getenv("DUE_DILIGENCE_REPORT_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            report_model=os.getenv("DUE_DILIGENCE_REPORT_MODEL", "deepseek-v4-pro"),
            report_max_tokens=max(1024, min(32768, int(os.getenv("DUE_DILIGENCE_REPORT_MAX_TOKENS", "8192")))),
            report_timeout_seconds=max(30, min(600, int(os.getenv("DUE_DILIGENCE_REPORT_TIMEOUT_SECONDS", "180")))),
            report_max_retries=max(0, min(5, int(os.getenv("DUE_DILIGENCE_REPORT_MAX_RETRIES", "2")))),
            report_repair_attempts=max(0, min(2, int(os.getenv("DUE_DILIGENCE_REPORT_REPAIR_ATTEMPTS", "1")))),
            report_reasoning_effort=report_reasoning_effort,
            report_fallback_model=os.getenv(
                "DUE_DILIGENCE_REPORT_FALLBACK_MODEL", "deepseek-flash").strip(),
            report_fallback_max_retries=max(0, min(3, int(os.getenv(
                "DUE_DILIGENCE_REPORT_FALLBACK_MAX_RETRIES", "1")))),
            s3_bucket=os.getenv("EMAIL_S3_BUCKET", ""),
            s3_endpoint=os.getenv("EMAIL_S3_ENDPOINT_URL") or None,
            s3_region=os.getenv("AWS_REGION", "us-east-1"),
            s3_prefix=os.getenv("EMAIL_S3_PREFIX", "nanoclaw/email").strip("/"),
            s3_kms_key=os.getenv("EMAIL_S3_KMS_KEY_ID", ""),
            smtp_enabled=os.getenv("INQUIRY_SMTP_ENABLED", "false").lower() == "true",
            auto_clarify=os.getenv("NANOCLAW_EMAIL_CLARIFICATION_AUTO_REPLY_ENABLED", "false").lower() == "true",
            demo_mode=os.getenv("DEMO_MODE", "false").lower() == "true",
            demo_prefer_fixtures=os.getenv("DEMO_PREFER_FIXTURES", "false").lower() == "true",
            demo_live_mailbox=os.getenv("DEMO_LIVE_MAILBOX", "false").lower() == "true",
            demo_external_timeout_seconds=max(2, min(30, int(os.getenv(
                "DEMO_EXTERNAL_TIMEOUT_SECONDS", "8")))),
            demo_external_retries=max(0, min(2, int(os.getenv(
                "DEMO_EXTERNAL_RETRIES", "1")))),
            demo_report_timeout_seconds=max(10, min(120, int(os.getenv(
                "DEMO_REPORT_TIMEOUT_SECONDS", "45")))),
        )
