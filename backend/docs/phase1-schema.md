# 数据同步重构 Phase 1 基础设施表

状态：设计稿与幂等 DDL 已产出，**尚未执行**。DDL 文件为
[`docs/phase1-schema.sql`](phase1-schema.sql)，必须经人工评审后另行执行。

## 设计摘要

### `sync_run`

一行对应一轮同步运行。`run_id` 是自增主键；`round_name` 标识作业/轮次；
`started_at`、`ended_at` 和 `status` 描述生命周期；`total`、`success`、`skip`、
`fail`、`network_fail` 保存可直接汇总和告警的计数。索引支持按作业名或状态查看最近运行。

计数语义由后续执行器统一：`total` 是本轮冻结的目标数，`success + skip + fail`
应在正常结束时等于 `total`；`network_fail` 是 `fail` 的错误分类子集，不额外加入总数。

### `fund_sync_state`

一行对应一只基金的一类任务，复合主键为 `(fund_code, task_kind)`。`watermark_date`
表示已经校验并持久化的**连续**水位；`gap_start/gap_end` 记录水位前方或历史扫描发现的
待补区间；`status/attempts/last_error` 保存最近状态，`updated_at` 自动维护更新时间。

水位不能仅因上游返回空结果而推进。后续实现必须在同一数据库事务中完成事实表 upsert、
数据校验、`sync_item` 状态更新和水位推进；事务失败时四者一起回滚。

### `sync_item`

统一承载 repair/retry/dead-letter 状态。`item_id` 是队列主键；`run_id` 可追溯最近关联的
运行；业务唯一键 `(fund_code, task_kind, gap_start, gap_end)` 使同一缺口可以幂等 upsert；
`status/attempts/error/next_retry_at` 支持重试、退避和人工接管。领取队列的关键索引为
`(status, next_retry_at, item_id)`。

`gap_start`、`gap_end` 设为 `NOT NULL` 是有意约束：MySQL 唯一索引允许多行 `NULL`，会破坏
缺口去重。没有自然区间的任务应把同一个逻辑日期同时写入两列。`run_id` 不设外键，避免
运行记录归档或分区策略反向阻塞 repair/dead-letter 的长期保留。

## 与现有表的关系

- `fund_nav` 继续是净值事实表，保留 `(fund_code, trade_date)` 唯一语义；三张新表只保存
  调度、进度和失败状态，不复制净值事实，也不把水位字段塞进 `fund_nav`。
- `fund_sync_state.watermark_date` 是从已提交事实推导并持久化的运行水位，不替代
  `fund_nav.trade_date`。需要对账时仍以事实表为准，发现断档后回写 gap，而不是盲目抬高水位。
- `nav_repair_queue` 在过渡期继续服务当前 nav/adj 修复脚本，本批不删表、不改结构、不迁数据、
  不切换消费者。`sync_item` 是后续跨 nav/detail/holdings/event/adj 的统一目标；切换时应先明确
  双写或一次性迁移策略，验证待处理数量一致后，再停止旧队列入队。

## 后续增量水位重构如何消费

1. 调度器插入 `sync_run(status='running')`，冻结本轮目标数。
2. 规划器按 `task_kind` 分页读取 `fund_sync_state`；不存在状态行时，从 `fund_nav` 等事实表
   聚合一次初始水位并写入状态表，之后不再逐基金重复扫描全历史。
3. 发现缺口时幂等 upsert `sync_item`。repair worker 按 `status + next_retry_at + item_id`
   领取任务，增加 `attempts`，失败后进入 `retry`，超过上限进入 `dead`。
4. 正常 worker 成功写入事实后，在同一事务内推进 `watermark_date`、清理/缩小 gap，并把
   `sync_item` 标记为 `success`；失败则保留水位并写 `error`。
5. 每轮结束时从 item 结果或执行器计数更新 `sync_run`，设置 `ended_at` 与最终 `status`。

## 幂等与执行注意事项

- 三张表均使用 `CREATE TABLE IF NOT EXISTS`；索引内联在建表语句中，因此重复执行不会重复建索引。
- `IF NOT EXISTS` 不会校验或修复已存在同名表的列差异。正式执行前必须先运行只读
  `SHOW CREATE TABLE`/`information_schema` 检查，若有冲突则单独编写并评审 `ALTER TABLE`。
- 本 DDL 不修改 `fund_nav`、`nav_repair_queue` 或任何业务数据。本交付批次只产出脚本，未执行建表。
