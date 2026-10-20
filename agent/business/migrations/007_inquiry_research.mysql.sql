-- Durable, resumable due-diligence execution records. MySQL 8 / InnoDB / utf8mb4.
CREATE TABLE IF NOT EXISTS inquiry_research_run (
    run_id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(100) NOT NULL,
    inquiry_id VARCHAR(64) NOT NULL,
    customer_id VARCHAR(64) NOT NULL,
    research_revision INT NOT NULL,
    status VARCHAR(24) NOT NULL,
    phase VARCHAR(40) NOT NULL,
    checkpoint_json LONGTEXT NOT NULL,
    report_model VARCHAR(160) NULL,
    fallback_used TINYINT(1) NOT NULL DEFAULT 0,
    error_code VARCHAR(160) NULL,
    started_at DOUBLE NOT NULL,
    updated_at DOUBLE NOT NULL,
    finished_at DOUBLE NULL,
    UNIQUE KEY uq_research_revision (tenant_id, inquiry_id, research_revision),
    KEY ix_research_status (tenant_id, status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS inquiry_research_step (
    step_record_id VARCHAR(64) PRIMARY KEY,
    run_id VARCHAR(64) NOT NULL,
    step_id VARCHAR(80) NOT NULL,
    tool VARCHAR(40) NOT NULL,
    query_hash VARCHAR(64) NOT NULL,
    query_summary LONGTEXT NOT NULL,
    status VARCHAR(32) NOT NULL,
    reason VARCHAR(500) NULL,
    evidence_count INT NOT NULL DEFAULT 0,
    attempt_count INT NOT NULL DEFAULT 0,
    started_at DOUBLE NULL,
    finished_at DOUBLE NULL,
    updated_at DOUBLE NOT NULL,
    UNIQUE KEY uq_research_step (run_id, step_id),
    KEY ix_research_step_status (run_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS inquiry_research_evidence (
    evidence_row_id VARCHAR(64) PRIMARY KEY,
    run_id VARCHAR(64) NOT NULL,
    evidence_id VARCHAR(96) NOT NULL,
    step_id VARCHAR(80) NULL,
    company_identity_id VARCHAR(64) NULL,
    source_type VARCHAR(40) NOT NULL,
    quality VARCHAR(40) NOT NULL,
    url LONGTEXT NULL,
    content_hash VARCHAR(64) NOT NULL,
    payload_json LONGTEXT NOT NULL,
    collected_at DOUBLE NOT NULL,
    UNIQUE KEY uq_research_evidence (run_id, evidence_id),
    KEY ix_research_evidence_source (run_id, source_type, quality)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
