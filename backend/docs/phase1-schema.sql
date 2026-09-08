-- iFund 数据同步重构 Phase 1 基础设施表（MySQL 方言）
--
-- 交付约束：本文件仅供评审和后续人工执行；本批次不得执行建表。
-- 幂等边界：CREATE TABLE IF NOT EXISTS 保证重复执行不会重建已有表；
-- 如已有同名但结构不同的表，本脚本不会自动 ALTER，必须先人工核对。

CREATE TABLE IF NOT EXISTS `sync_run` (
    `run_id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '同步运行 ID',
    `round_name` VARCHAR(96) NOT NULL COMMENT '轮次/作业名，例如 daily_fund_nav',
    `status` VARCHAR(16) NOT NULL DEFAULT 'running' COMMENT 'running/success/partial/failed/aborted',
    `started_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    `ended_at` DATETIME(6) NULL,
    `total` INT UNSIGNED NOT NULL DEFAULT 0,
    `success` INT UNSIGNED NOT NULL DEFAULT 0,
    `skip` INT UNSIGNED NOT NULL DEFAULT 0,
    `fail` INT UNSIGNED NOT NULL DEFAULT 0,
    `network_fail` INT UNSIGNED NOT NULL DEFAULT 0,
    PRIMARY KEY (`run_id`),
    KEY `ix_sync_run_round_started` (`round_name`, `started_at`),
    KEY `ix_sync_run_status_started` (`status`, `started_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='每轮同步的生命周期与汇总统计';

CREATE TABLE IF NOT EXISTS `fund_sync_state` (
    `fund_code` VARCHAR(10) NOT NULL,
    `task_kind` VARCHAR(32) NOT NULL COMMENT 'nav/cum_return/detail/holdings/event/adj 等',
    `watermark_date` DATE NULL COMMENT '已验证并持久化的连续水位',
    `gap_start` DATE NULL COMMENT '当前已知缺口起点',
    `gap_end` DATE NULL COMMENT '当前已知缺口终点',
    `status` VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT 'pending/running/success/empty/failed/dead',
    `attempts` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '当前失败周期内累计尝试次数',
    `last_error` TEXT NULL,
    `updated_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (`fund_code`, `task_kind`),
    KEY `ix_fund_sync_state_kind_status_watermark` (`task_kind`, `status`, `watermark_date`),
    KEY `ix_fund_sync_state_status_updated` (`status`, `updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='每只基金、每类同步任务的持久化增量水位';

CREATE TABLE IF NOT EXISTS `sync_item` (
    `item_id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '统一 repair/dead-letter 项 ID',
    `run_id` BIGINT UNSIGNED NULL COMMENT '最近关联的 sync_run.run_id；不设外键以便独立保留/归档',
    `fund_code` VARCHAR(10) NOT NULL,
    `task_kind` VARCHAR(32) NOT NULL COMMENT 'nav/cum_return/detail/holdings/event/adj 等',
    `gap_start` DATE NOT NULL COMMENT '待处理区间起点；非区间任务填写逻辑日期',
    `gap_end` DATE NOT NULL COMMENT '待处理区间终点；非区间任务与 gap_start 相同',
    `status` VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT 'pending/running/retry/success/dead',
    `attempts` INT UNSIGNED NOT NULL DEFAULT 0,
    `error` TEXT NULL COMMENT '最近一次失败摘要',
    `next_retry_at` DATETIME(6) NULL,
    `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    `updated_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (`item_id`),
    UNIQUE KEY `uq_sync_item_fund_kind_gap` (`fund_code`, `task_kind`, `gap_start`, `gap_end`),
    KEY `ix_sync_item_status_retry` (`status`, `next_retry_at`, `item_id`),
    KEY `ix_sync_item_fund_kind_status` (`fund_code`, `task_kind`, `status`),
    KEY `ix_sync_item_run_status` (`run_id`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='跨任务统一的修复、重试与死信状态';
