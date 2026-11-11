-- Explicit deployment migration; never execute from HTTP/startup.
-- Portable table DDL; use product_worker migrate to create/check secondary indexes.

CREATE TABLE IF NOT EXISTS open_fund_product_batch (
    batch_id VARCHAR(32) PRIMARY KEY, product_id INTEGER NOT NULL,
    scope_kind VARCHAR(20) NOT NULL, scope_key VARCHAR(128) NOT NULL,
    window_start VARCHAR(10) NOT NULL, window_end VARCHAR(10) NOT NULL,
    source_name VARCHAR(128) NOT NULL, method_version VARCHAR(128) NOT NULL,
    evidence_sha256 VARCHAR(64) NOT NULL, evidence_uri VARCHAR(2048) NOT NULL,
    status VARCHAR(16) NOT NULL, coverage VARCHAR(16) NOT NULL,
    row_count INTEGER NOT NULL, recorded_at DOUBLE NOT NULL,
    capabilities_json LONGTEXT NOT NULL);

CREATE TABLE IF NOT EXISTS open_fund_product_fact (
    batch_id VARCHAR(32) NOT NULL, product_id INTEGER NOT NULL,
    record_key VARCHAR(64) NOT NULL, fund_code VARCHAR(6), event_date VARCHAR(10),
    manager_name VARCHAR(128), industry_code VARCHAR(64), concept_code VARCHAR(64),
    benchmark_code VARCHAR(64), etf_flag INTEGER, payload_json LONGTEXT NOT NULL,
    PRIMARY KEY (batch_id,record_key));
