"""Explicit offline schema for full ETF denominator and atomic mapping versions."""
from __future__ import annotations

MAPPING_METHODS = {142: 'annual-full-domestic-portfolio-current-sw1-gross-assets-v1',
                   143: 'full-domestic-etf-portfolio-current-cls-nav-association-v1'}

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS open_fund_etf_directory (
    version_id VARCHAR(32) PRIMARY KEY,cohort_hash VARCHAR(64) NOT NULL,source_snapshot VARCHAR(32) NOT NULL,
    observed_at DOUBLE NOT NULL,recorded_at DOUBLE NOT NULL,valid_until DOUBLE NOT NULL,row_count INTEGER NOT NULL,
    payload_json LONGTEXT NOT NULL,payload_hash VARCHAR(64) NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS open_fund_etf_generation (
    generation_id VARCHAR(32) PRIMARY KEY,product_id INTEGER NOT NULL,cohort_hash VARCHAR(64) NOT NULL,
    directory_version VARCHAR(32) NOT NULL,observed_at DOUBLE NOT NULL,valid_until DOUBLE NOT NULL,
    scope_count INTEGER NOT NULL,eligible_count INTEGER NOT NULL,undisclosed_count INTEGER NOT NULL,
    not_applicable_count INTEGER NOT NULL,fact_count INTEGER NOT NULL,payload_bytes BIGINT NOT NULL,
    method_version VARCHAR(128) NOT NULL,evidence_hash VARCHAR(64) NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS open_fund_etf_generation_scope (
    generation_id VARCHAR(32) NOT NULL,fund_code VARCHAR(6) NOT NULL,source_version VARCHAR(32) NOT NULL,
    eligibility_status VARCHAR(20) NOT NULL,row_count INTEGER NOT NULL,PRIMARY KEY(generation_id,fund_code))""",
    """CREATE TABLE IF NOT EXISTS open_fund_etf_generation_fact (
    generation_id VARCHAR(32) NOT NULL,fund_code VARCHAR(6) NOT NULL,association_code VARCHAR(64) NOT NULL,
    annual_weight DOUBLE NOT NULL,payload_json LONGTEXT NOT NULL,payload_hash VARCHAR(64) NOT NULL,
    PRIMARY KEY(generation_id,fund_code,association_code))""",
)
