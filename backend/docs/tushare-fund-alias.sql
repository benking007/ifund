-- iFund Tushare 渠道冲突 alias 表（additive；不修改 fund_ts_code_map）
CREATE TABLE IF NOT EXISTS `fund_ts_code_alias` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `fund_code` VARCHAR(10) NOT NULL COMMENT 'iFund 六位代码',
    `primary_ts_code` VARCHAR(20) NOT NULL COMMENT 'fund_ts_code_map 当前主角色',
    `alias_ts_code` VARCHAR(20) NOT NULL COMMENT '同六位码的附加 Tushare 角色',
    `primary_channel` VARCHAR(4) NOT NULL COMMENT 'OF/SH/SZ',
    `alias_channel` VARCHAR(4) NOT NULL COMMENT 'OF/SH/SZ',
    `source` VARCHAR(32) NOT NULL DEFAULT 'tushare.fund_basic',
    `source_evidence` LONGTEXT NULL COMMENT 'fund_basic 多 ts_code 证据 JSON',
    `status` VARCHAR(16) NOT NULL DEFAULT 'active',
    `verified_at` DATETIME(6) NOT NULL,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_fund_ts_code_alias_business` (`fund_code`, `alias_ts_code`),
    KEY `ix_fund_ts_code_alias_alias` (`alias_ts_code`),
    KEY `ix_fund_ts_code_alias_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Tushare 同六位码多渠道角色；主映射语义保持不变';
