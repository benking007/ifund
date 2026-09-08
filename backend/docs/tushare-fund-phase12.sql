-- iFund Tushare 基金 Phase 1+2 additive DDL（映射层 + 分红扩展列）
-- 幂等：CREATE TABLE IF NOT EXISTS / ADD COLUMN 仅在列不存在时执行（MySQL 8 需手工检查列）
-- 不修改 funds 核心字段；不删除 fund_div_split 既有 5 行。

CREATE TABLE IF NOT EXISTS `fund_ts_code_map` (
    `fund_code` VARCHAR(10) NOT NULL COMMENT 'iFund 六位码，对齐 funds.code',
    `ts_code` VARCHAR(20) NOT NULL COMMENT 'Tushare ts_code，含 .OF/.SH/.SZ',
    `market` CHAR(1) NOT NULL COMMENT 'E=场内 O=场外',
    `tushare_name` VARCHAR(255) DEFAULT NULL,
    `tushare_status` VARCHAR(8) DEFAULT NULL COMMENT 'L/D/I',
    `match_kind` VARCHAR(32) NOT NULL DEFAULT 'prefix_exact'
        COMMENT 'prefix_exact/share_class/ambiguous_resolved',
    `source` VARCHAR(32) NOT NULL DEFAULT 'fund_basic',
    `verified_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (`fund_code`),
    UNIQUE KEY `uq_fund_ts_code_map_ts_code` (`ts_code`),
    KEY `ix_fund_ts_code_map_market` (`market`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='六位码↔Tushare ts_code 权威映射（additive，不改 funds 表）';

CREATE TABLE IF NOT EXISTS `fund_ts_code_quarantine` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `entity_kind` VARCHAR(32) NOT NULL
        COMMENT 'ifund_unmatched/tushare_orphan/ambiguous',
    `code` VARCHAR(20) NOT NULL COMMENT '六位码或 ts_code',
    `reason` VARCHAR(255) NOT NULL DEFAULT '',
    `detail_json` TEXT NULL,
    `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_fund_ts_quarantine_kind_code` (`entity_kind`, `code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='映射未匹配/歧义隔离区，禁止臆造 ts_code';

-- fund_div_split 扩展列（保留 UNIQUE(fund_code,ex_date,event_type) 与既有 5 行）
-- 若列已存在则跳过（脚本侧用 information_schema 判断）
