-- Explicit deployment migration; never execute from HTTP/startup.
-- MySQL 8 and SQLite >= 3.35. Not idempotent: each statement fails if the
-- column is already gone. `python -m app.open_data.product_worker migrate`
-- performs the same drops only where the column still exists.

ALTER TABLE open_fund_base_watermark DROP COLUMN valid_until;
ALTER TABLE open_fund_product_batch DROP COLUMN valid_until;
ALTER TABLE open_fund_manager_directory_batch DROP COLUMN valid_until;
ALTER TABLE open_fund_concept_generation DROP COLUMN valid_until;
ALTER TABLE open_fund_disclosure_eligibility DROP COLUMN valid_until;
ALTER TABLE open_fund_etf_directory DROP COLUMN valid_until;
ALTER TABLE open_fund_etf_generation DROP COLUMN valid_until;
