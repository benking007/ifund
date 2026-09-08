-- iFund Tushare 基金 Phase 3 additive DDL（公司 / 基础扩展 / 份额）
-- 只新增表，不覆盖 funds / fund_details 核心字段，也不删除历史数据。

CREATE TABLE IF NOT EXISTS `fund_company` (
    `company_id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `name` VARCHAR(255) NOT NULL COMMENT 'Tushare 基金公司全称',
    `short_name` VARCHAR(128) NULL,
    `short_en_name` VARCHAR(255) NULL,
    `province` VARCHAR(64) NULL,
    `city` VARCHAR(64) NULL,
    `address` VARCHAR(500) NULL,
    `phone` VARCHAR(128) NULL,
    `office` VARCHAR(255) NULL,
    `website` VARCHAR(500) NULL,
    `chairman` VARCHAR(128) NULL,
    `general_manager` VARCHAR(128) NULL,
    `registered_capital` DECIMAL(24,6) NULL,
    `setup_date` DATE NULL,
    `end_date` DATE NULL,
    `employees` INT NULL,
    `main_business` TEXT NULL,
    `org_code` VARCHAR(64) NULL COMMENT 'Tushare 官方组织机构代码',
    `credit_code` VARCHAR(64) NULL COMMENT 'Tushare 官方统一社会信用代码',
    `source` VARCHAR(32) NOT NULL DEFAULT 'tushare',
    `fetched_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (`company_id`),
    UNIQUE KEY `uq_fund_company_name` (`name`),
    KEY `ix_fund_company_org_code` (`org_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='Tushare 基金管理公司维表；上游无 ts_code，使用 company_id 关联';

CREATE TABLE IF NOT EXISTS `fund_basic_ext` (
    `fund_code` VARCHAR(10) NOT NULL COMMENT '关联 funds.code 的六位码',
    `ts_code` VARCHAR(20) NOT NULL,
    `company_id` BIGINT UNSIGNED NULL,
    `name` VARCHAR(255) NULL,
    `management` VARCHAR(255) NULL,
    `custodian` VARCHAR(255) NULL,
    `market` CHAR(1) NULL COMMENT 'E=场内 O=场外',
    `status` CHAR(1) NULL COMMENT 'L/D/I',
    `fund_type` VARCHAR(128) NULL,
    `invest_type` VARCHAR(128) NULL,
    `fund_category` VARCHAR(128) NULL COMMENT 'Tushare type 字段',
    `trustee` VARCHAR(255) NULL,
    `found_date` DATE NULL,
    `due_date` DATE NULL,
    `list_date` DATE NULL,
    `issue_date` DATE NULL,
    `delist_date` DATE NULL,
    `purchase_start_date` DATE NULL,
    `redemption_start_date` DATE NULL,
    `issue_amount` DECIMAL(24,6) NULL,
    `management_fee` DECIMAL(12,6) NULL,
    `custodian_fee` DECIMAL(12,6) NULL,
    `duration_years` DECIMAL(12,4) NULL,
    `par_value` DECIMAL(20,6) NULL,
    `minimum_amount` DECIMAL(24,6) NULL,
    `expected_return` DECIMAL(20,8) NULL,
    `benchmark` TEXT NULL,
    `source` VARCHAR(32) NOT NULL DEFAULT 'tushare',
    `fetched_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (`fund_code`),
    UNIQUE KEY `uq_fund_basic_ext_ts_code` (`ts_code`),
    KEY `ix_fund_basic_ext_company` (`company_id`),
    KEY `ix_fund_basic_ext_market_status` (`market`, `status`),
    CONSTRAINT `fk_fund_basic_ext_company`
      FOREIGN KEY (`company_id`) REFERENCES `fund_company` (`company_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='fund_basic 权威结构化补丁；不 UPDATE funds 核心字段';

CREATE TABLE IF NOT EXISTS `fund_share` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `fund_code` VARCHAR(10) NOT NULL COMMENT '关联 funds.code 的六位码',
    `ts_code` VARCHAR(20) NOT NULL,
    `trade_date` DATE NOT NULL,
    `share_type` VARCHAR(32) NOT NULL DEFAULT 'fund_total',
    `share_value` DECIMAL(28,6) NOT NULL,
    `share_unit` VARCHAR(32) NOT NULL DEFAULT '10k_shares',
    `source_field` VARCHAR(32) NOT NULL DEFAULT 'fd_share',
    `source` VARCHAR(32) NOT NULL DEFAULT 'tushare',
    `fetched_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_fund_share_ts_date_type` (`ts_code`, `trade_date`, `share_type`),
    KEY `ix_fund_share_code_date` (`fund_code`, `trade_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='基金份额历史；share_value 对应 Tushare fd_share（万份）';

CREATE TABLE IF NOT EXISTS `fund_share_sync_state` (
    `ts_code` VARCHAR(20) NOT NULL,
    `requested_start` DATE NULL,
    `requested_end` DATE NULL,
    `status` VARCHAR(16) NOT NULL,
    `row_count` INT UNSIGNED NOT NULL DEFAULT 0,
    `attempts` INT UNSIGNED NOT NULL DEFAULT 0,
    `last_error` TEXT NULL,
    `updated_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (`ts_code`),
    KEY `ix_fund_share_sync_status` (`status`, `updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='fund_share 全量回填断点与失败记录';
