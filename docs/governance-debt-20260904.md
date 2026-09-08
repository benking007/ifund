# iFund 剩余治理债清理（2026-09-04）

## 1. 交易日历

- 主源改为 Tushare `trade_cal`，固定 `exchange=SSE`。`trade_dates` 是无交易所字段的统一日历，
  沪深法定休市安排一致，故不重复请求 SZSE。
- 每轮按自然年分页，刷新滚动九年窗口；2026 年运行窗口为 `2020-01-01~2028-12-31`，
  固定 9 次 Tushare 调用。窗口外既有历史保留，最终仍通过事务内 `replace_all` 全量替换。
- Tushare 失败时保留已有行并降级到 AkShare/Sina；旧源没有删除。
- API、CLI、cron 共用 `app.trade_calendar.service.sync_calendar()`，避免三套同步语义。
- 自动任务：`/etc/cron.d/ifund-trade-calendar`，每月 1 日 07:15，带 `flock`、10 分钟 timeout、
  独立日志与 JSON 证据。仓库 manifest 为 `deploy/ifund-trade-calendar.cron`。
- 生产实跑：9 次调用，表由 8,797 行更新为 9,041 行，保留 7,100 条窗口外历史；
  2026 年 242 日，09-25 与 10-01~10-07 均不在表中，10-08 在表中；2027 年 244 日。
- 上游边界：2028 年对 SSE 和 SZSE 的 `trade_cal` 均返回 0 行，因此未伪造尚未发布的法定休市安排；
  当前最大日期为 2027-12-31。同步结果会显式给出 `missing_years=[2028]`，月度任务自动重试。

## 2. 净值完整性告警

- 现有运行环境没有可复用的 IM/邮件/webhook 告警实现；继续使用现有 cron、日志和 JSON 证据，
  不新建监控平台。
- `app.fund_nav.alerts` 在 `finalize` / `reconcile` 数据工作结束后统一检查：
  覆盖率低于十交易日基线 80%；连续至少 2 个交易日无事实更新；黑名单单日新增大于 50；
  repair 未闭环最老任务超过 24 小时；水位指向的事实行不存在且数量大于 0。
- 输出：`backend/logs/nav_alerts/latest.json`；有告警时另写时间戳事件 JSON，并记录唯一
  `NAV_ALERT {...}` ERROR 行。数据和证据先落地，再返回退出码 1；执行异常返回 2。
- 外部告警挂点：`app.fund_nav.alerts.emit()`。未来在该函数落盘后接 IM/邮件 dispatcher，
  不需要改变判据或调度入口。
- 生产只读探针：覆盖率 89.98%、连续无更新 0、黑名单新增 0、水位事实不一致 0；
  repair 开放 3,468 条、最老 111 小时，准确触发 `nav_repair_queue_overage`。

## 3. Tushare 渠道冲突 alias

- 新增 additive 表 `fund_ts_code_alias`，字段包含六位码、主/别名 ts_code、主/别名渠道、
  `tushare.fund_basic` 证据 JSON、状态和核验时间。业务唯一键是 `(fund_code, alias_ts_code)`。
- 从既有 `fund_ts_code_quarantine(entity_kind=ambiguous, reason=multiple_ts_codes)` 重建，
  该证据由权威 `fund_basic` 全量映射过程产生，不再消耗 Tushare 调用。
- 生产连续执行两次：均构建/存储 63 行，其中 `OF/SZ=56`、`OF/SH=7`；
  `fund_ts_code_map` 始终为 27,208 行，执行前后 SHA-256 指纹一致。
- 角色规则：默认主角色继续服从既有 `_pick_ts_code`（这 63 条均为 E 场内 `.SZ/.SH`）；
  只有明确的场外净值/申赎上下文才选 `.OF` alias，交易所行情/份额使用场内角色。
  当前基金详情/净值 API 以六位码返回聚合事实，没有角色切换参数，本批不扩大读路径改造。

## 验证、部署和回滚

- 定向测试 83 passed、2 subtests passed，覆盖主源年度分页、失败兜底、替换服务、五项告警边界、
  告警文件、非零退出和 alias 幂等。
- Pylint `app` 保持 10.00/10；本批 14 个 Python 文件通过 Ruff check 与 format check。
  全仓 Ruff 仍报告 187 条本批前已存在的跨模块存量，本批没有越界批量改写。
- iFund 服务未重启：alias 是独立表，日历 cron 是独立进程，净值 cron 下次启动自动加载新代码。
- 回滚顺序：移除 `/etc/cron.d/ifund-trade-calendar` 停止自动日历；回退本批代码后用旧
  `fetch calendar` 重拉 Sina 日历；告警只需回退 `daily_nav_sync.py` 的调用点；alias 表未接读路径，
  可原地保留，确认不再需要后再单独 `DROP TABLE fund_ts_code_alias`。

证据：

- `backend/logs/trade_calendar_sync/latest.json`
- `backend/logs/governance/trade-calendar-verification-20260904.json`
- `backend/logs/governance/fund-ts-code-alias-latest.json`
- `backend/logs/nav_alerts/latest.json`
- `backend/logs/nav_alerts/nav-alert-20260904T135024+0800.json`
