# Changelog

本项目的所有重要变更均记录于此文件。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增

- **AI 定性分析接入 agim RPC**：新增 `backend/app/ai_analyze/rpc_client.py`，通过 Unix socket 调用 agim 的 `llm_complete` 工具；默认模型切换为 `deepseek-v4-flash`（可通过 `IFUND_LLM_BACKEND` 覆盖）
- **持仓拉取并发化**：`backend/cli/fetch.py` 引入 `ThreadPoolExecutor`（`IFUND_CLI_CONCURRENCY`，默认 4），单线程逐只拉取改为并发；实测速率从 0.6 只/分钟提升至 131 只/分钟（约 200 倍）
- **Worker 基础框架**：新增 `backend/app/common/worker_base.py`，统一 worker 子进程主循环（确定基金集合 + 进程/线程池并发 + 进度上报 + 协作式终止），各模块 worker 只需实现 `process_one(code)`
- **前端主题同步与嵌入模式**：新增 `useDashboardTheme.ts`（与外部 Dashboard 主题同步）、`embed.tsx`（MemoryRouter 嵌入入口）、`config.ts`（`APP_BASE` 基础路径）
- **基金详情弹窗「重新分析」按钮**：已有 AI 定性分析的基金可在详情弹窗一键重跑（SSE 流式消费）
- **行业映射北交所补采**：`em_worker.py` 支持北交所 920 段，优先东财个股接口，失败回退巨潮（cninfo）备用源
- **持仓分批拉取脚本**：`backend/scripts/holdings_batch*.sh` 三套串联脚本（按类型分批、单批失败继续、BEFORE/AFTER 覆盖数统计、绝对路径参数化）
- **部署模板**：`deploy/ifund.service` systemd 单元模板（`{{IFUND_BACKEND_DIR}}` 等占位符，机器路径不硬编码）
- **集成文档**：`INTEGRATION.md` 说明与外部 Dashboard 的集成部署方式

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
