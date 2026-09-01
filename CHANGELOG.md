# Changelog

本项目的所有重要变更均记录于此文件。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增（2026-08-30 ~ 09-01）

- **净值数据治理 M1–M6 落地**（2026-08-30）：`fund_nav` 增加 `adj_nav`/`adj_src`（Tushare 复权 + 本地自算前复权，`adj_engine.py`）；`fund_div_split`（分红/拆分事件，`events_worker.py`）；`nav_repair_queue`（缺口修复队列，`repair_crud.py`/`backfill_worker.py`）；`/api/stats` 新增复权覆盖率等指标。详见 `docs/nav_governance_delivery.md`
- **蛋卷不收录基金占位机制**：`fund_details` 对 1164 只蛋卷无数据基金（后端份额/定期开放债/部分 FOF 联接/货币B）写 `source_unavailable` 占位，7 天内跳过拉取、7 天后自动重探（`scripts/mark_source_unavailable.py` + `detail_crud._is_source_unavailable`），避免每日 cron 空拉
- **detail 每日刷新 cron**：`10 20 * * *` `ifund_cli.py fetch detail`（is_expired 去重，与 20:00 净值任务错开）
- **`fund_ai_analyze` MCP 工具**（myfund 侧 `scripts/ifund_mcp.mjs`）：CLI 子进程方式（`ifund_cli.py ai-analyze batch --json` 直连 data.db 免 JWT），同步触发基金 AI 定性分析
- **MySQL 后端**（`app/db/mysql.py`）：PyMySQL 驱动，完整实现 Database 契约（方言转换/upsert/batch/三复杂查询），`DB_BACKEND=mysql` 切换；DDL 由 `_convert_schema_sql` 从 `schema_sqlite.sql` 实时转换，无需独立 schema_mysql.sql
- **数据迁移脚本**：`scripts/migrate_sqlite_to_mysql.py`（分片游标 + checkpoint 断点续跑 + 每表对账）
- **集成文档与部署**：`INTEGRATION.md`、`deploy/ifund.service` systemd 模板

### 变更

- **废弃 `fund_nav_return` 中间表**（2026-09-01，用户拍板）：myfund 自选/估算区间收益不再经中间表缓存，改为**并发直读 `fund_details.return_3m/6m/1y`**（`fetch_fund_returns_many`，asyncio.gather 调 ifund_detail）；`fund_nav.py` 删除 fund_return_worker，schema 移除表定义——`fund_details` 是区间收益唯一事实源
- **scale 弹性处理**：`fund_details.scale` 允许为空（8637 只蛋卷不返回规模），不再因缺 scale 判 is_expired 每日重拉；消费端名称走其他源
- **数据源迁移 SQLite → MySQL**（2026-09-01）：9.2GB data.db 全量 4161 万行迁至内网 MySQL（192.168.0.9/ifund，与 myfund 同服务器）；凭据 `/etc/ifund-prod.env`（chmod 600）+ systemd 双 EnvironmentFile + cron `set -a` 注入（与 myfund/fin-data 同方案）；服务端口 :8003，systemd 托管
- **git remote 切换**：推送目标从 `OrangesHuang/ifund`（无写权限）切换为 fork `benking007/ifund`

### 修复

- **danjuanfunds 请求超时防卡死**：fetch detail 加 8s 超时
- **worker_base.main() 缺失**：批量任务子进程崩溃修复 + fund_nav 重试退避
- **MySQL UNIQUE 组合键超长**：fund_holdings 4 列 UNIQUE 超 3072 字节 → 转换器对约束列强制 VARCHAR(64)
- **MySQL 保留字**：列名 `key`（app_settings）统一加反引号

### 新增（2026-08-02）

- **AI 定性分析接入 agim RPC**：新增 `backend/app/ai_analyze/rpc_client.py`，通过 Unix socket 调用 agim 的 `llm_complete` 工具；默认模型切换为 `deepseek-v4-flash`（可通过 `IFUND_LLM_BACKEND` 覆盖）
- **持仓拉取并发化**：`backend/cli/fetch.py` 引入 `ThreadPoolExecutor`（`IFUND_CLI_CONCURRENCY`，默认 4），单线程逐只拉取改为并发；实测速率从 0.6 只/分钟提升至 131 只/分钟（约 200 倍）
- **Worker 基础框架**：新增 `backend/app/common/worker_base.py`，统一 worker 子进程主循环（确定基金集合 + 进程/线程池并发 + 进度上报 + 协作式终止），各模块 worker 只需实现 `process_one(code)`
- **前端主题同步与嵌入模式**：新增 `useDashboardTheme.ts`（与外部 Dashboard 主题同步）、`embed.tsx`（MemoryRouter 嵌入入口）、`config.ts`（`APP_BASE` 基础路径）
- **基金详情弹窗「重新分析」按钮**：已有 AI 定性分析的基金可在详情弹窗一键重跑（SSE 流式消费）
- **行业映射北交所补采**：`em_worker.py` 支持北交所 920 段，优先东财个股接口，失败回退巨潮（cninfo）备用源
- **持仓分批拉取脚本**：`backend/scripts/holdings_batch*.sh` 三套串联脚本（按类型分批、单批失败继续、BEFORE/AFTER 覆盖数统计、绝对路径参数化）
- **部署模板**：`deploy/ifund.service` systemd 单元模板（`{{IFUND_BACKEND_DIR}}` 等占位符，机器路径不硬编码）
- **集成文档**：`INTEGRATION.md` 说明与外部 Dashboard 的集成部署方式

### 工程加固（2026-08-02 严格 CR）

- **后端事务原子化（P0）**：`db/sqlite.py` 新增 `transaction()` 上下文管理器（BEGIN IMMEDIATE + savepoint 嵌套）；`holdings_crud.upsert` / `industry_crud.upsert_industry` 多步写入原子化
- **表名白名单**：`VALID_TABLES` + `_check_table()` 六入口校验，消除表名 f-string 拼接面
- **认证加固**：`/login` `/register` 内存滑动窗口限速（5 次/60s/IP + Retry-After）；`UserCreate.password` 最小长度 8
- **拉取健壮性**：退避 ±30% jitter、线程局部 Session 复用、空列名漂移防护、`ProcessPool.cancel()` 返回值检查、进度每 10 只批量、并发钳制 [1,64]
- **前端类型门禁（P0）**：21 个 TS 错误清零（6 文件），`tsc --noEmit` 0 错误
- **前端请求统一与竞态**：新增 `rawFetch.ts`（token 注入 + 401 清 token 跳登录）；搜索 debounce；requestId 序列校验；AbortController 全覆盖（5 页面）；MirrorView AI 流旧流终止
- **前端工具链**：eslint 补全（0 error/0 warning）；路由懒加载（主 chunk 1.86MB → 1.15KB 入口 + 分包）
- **依赖与基线**：requirements 固定（akshare==1.18.81 / pandas==3.0.5 / requests==2.34.2）；pytest 17 用例通过
- **索引精简**：删除冗余 `ix_fund_holdings_fund_code`；`common_days_ratio` 回测诊断字段

### 修复

- **行业映射统计口径**：`industry_crud.py` 新增 `_is_a_stock()` / `_is_hk_stock()`，`stats()` / `uncovered_held()` 只统计真实 A 股 + 港股股票，排除债券、可转债、场内基金、海外股（韩股等）；未覆盖口径从虚高的 4590 条修正为真实缺口
- **港股行业映射补采**：港股 560 只（QDII/沪港深持仓扩充后新增）通过东财个股接口补采完成
- **东财接口超时与重试**：`fund_holdings/fetch/worker.py` 增加请求 timeout（15s）与指数退避重试（2/4/8/16s，最多 5 次），避免单次超时中断整批
- **持仓缓存守卫**：`holdings_crud.py` 空响应不再写入 2 字节 `[]` 缓存文件（此前导致管道瘫痪约 1 周）

## [0.1.0] - 2026-06-24

### 新增

- **初始架构**（2026-06-14 ~ 06-16）：公募基金筛选与数据管理系统，Flask 3.1 + SQLite + React（Ant Design）；akshare 多数据源拉取基金名单/详情/持仓/净值/交易日历
- **数据库抽象层**：`app/db/` 可插拔后端设计（`DB_BACKEND=sqlite` / 未来 `mysql`），PostgREST 风格过滤 DSL（`eq.` / `gt.` / `lt.` / `ilike.`）
- **Worker 子进程架构**：异步批量拉取（子进程隔离）、前端轮询进度、可终止任务（`fetch_tasks` 表）
- **基金筛选**：多区间条件（规模/夏普/回撤/仓位）+ 多列排序 + 分页；`/api/fund/list`
- **预设（Preset）**：查询条件保存/加载/镜像快照重建，可驱动批量拉取
- **组合分析**（2026-06-16）：行业暴露聚类（`cluster`）、仓位建议生成（`position`，聚类+TOP 加权评分）
- **实盘对账**（2026-06-17）：`reconcile` 模块，镜像基金 vs 实盘持仓对账
- **认证**：JWT 登录注册 + PAT 令牌（`/api/auth/tokens`，供外部 agent 集成）
- **永续组合**（2026-07 上旬）：`perpetual` 模块，组合择时策略（见 `docs/perpetual_timing_strategy.md`）
- **历史回测**：`historical` 模块（backtest / perpetual_backtest / quarter / screen）
- **MCP 服务**：`mcp_server/` FastMCP 单工具暴露（33 工具 → 1 重构）
- **CLI**：`backend/cli/` 12 个子模块（同步拉取、持仓、净值回填等），入口 `ifund_cli.py`
- **文档**：README、ARCHITECTURE、算法说明（仓位建议/聚类）、AI 分析 prompt、集成指南
