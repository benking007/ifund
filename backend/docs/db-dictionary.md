# iFund 数据库字典（MySQL，2026-09-01 迁移后）

> 生成：2026-09-01 · 生产库：`MySQL 192.168.0.9/ifund`（`DB_BACKEND=mysql`，凭据 `/etc/ifund-prod.env`）
> 旧 SQLite：`backend/data.db`（保留作回滚，结构与本字典一致）
> 目的：全量盘点 24 张表，明确每张表的**用途 / 数据源 / 关键字段 / 消费方**，
> 作为「基金数据唯一事实源」的参照，避免后续重复建设同类数据。
> 约定：新增任何基金/净值/持仓/收益类数据，先查本字典；已有表能覆盖的不得另起炉灶。

---

## 0. 数据域总览（按用途分组）

| 域 | 表 | 说明 |
|---|---|---|
| 基础 | `funds`, `fund_types`, `trade_dates`, `stock_industry` | 基金主档、类型、交易日历、行业映射 |
| 净值 | `fund_nav`（3394万行）, `fund_cum_return`（577万行）, `fund_div_split` | 单位/复权净值、累计收益、分红拆分 |
| 详情 | `fund_details`（27408行） | 蛋卷快照：规模/经理/仓位/区间收益/年度收益（93列） |
| 持仓 | `fund_holdings`（167万行） | 季报股票/债券持仓 |
| 派生分析 | `fund_ai_analysis`（5421行）, `fund_etf_linkage`（1000行）, `fund_manager_tenure`（86210行） | AI 解读、ETF 血缘、经理任职史 |
| 任务/状态 | `fetch_tasks`, `event_scan_status`, `nav_repair_queue` | 拉取任务、分红扫描状态、净值修复队列 |
| 用户/业务 | `users`, `api_tokens`, `app_settings` | 认证与配置 |
| 组合/自选 | `portfolios`, `user_holdings`, `holding_txns`, `perpetual_portfolio`, `query_presets`, `fund_snapshots` | 实盘组合、预设快照 |

---

## 1. 基础域

### funds（27408 行）— 基金主档
- **用途**：全部基金代码/名称/类型/拼音检索；`resolve_codes` 全量目标源
- **数据源**：天弘/蛋卷列表同步（`ifund_cli fetch` 上游）
- **关键字段**：`code`(PK语义), `name`, `type`(如 混合型-灵活), `fund_type`(stock/non_stock), `pinyin_abbr/full`(检索)
- **消费方**：全系统；CLI `--types` 过滤、`/fund/list`

### fund_types（33 行）— 基金类型字典
- **用途**：类型 → 大类（category）映射，用于分组筛选
- **样例**：`混合型-偏股 → stock`

### trade_dates（8797 行）— 交易日历
- **用途**：`base_trade_date()` 保守基准交易日；跳过判据核心
- **数据源**：`cmd_calendar`（fetch_trade_dates）

### stock_industry（5927 行）— 股票行业映射
- **用途**：持仓股票 → 申万 L1/L2/L3 + 东财行业；基金持仓行业归因
- **数据源**：`cmd_industry`（sw / em 两模式）

---

## 2. 净值域（最核心，体积最大）

### fund_nav（33,947,920 行）— 日净值主表（唯一净值事实源）
- **用途**：单位净值 nav / 累计净值 acc_nav / 日涨幅 daily_return / **复权净值 adj_nav**（区间收益兜底计算）
- **数据源**：iFund 净值同步（akshare/eastmoney）+ Tushare 兜底；`adj_nav` 由 adj_engine 用分红拆分事件补齐
- **关键字段**：`fund_code`, `trade_date`, `nav`, `acc_nav`, `daily_return`, `adj_nav`, `adj_src`
- **消费方**：`load_nav_history`（区间收益兜底）、七日年化、净值曲线、回测
- ⚠️ 3394 万行，全量 27408 只 × 历史交易日；**勿重复建净值表**

### fund_cum_return（5,772,753 行）— 累计收益率
- **用途**：净值 → 累计收益率序列（回测/绩效用）
- **关键字段**：`fund_code`, `trade_date`, `cum_return`

### fund_div_split（5 行）— 分红拆分事件
- **用途**：adj_nav 复权计算的事件源（div 分红 / split 拆分）
- **数据源**：Tushare `fund_div`；`event_scan_status` 记录扫描进度

---

## 3. 详情域

### fund_details（27408 行 / 93 列）— 基金详情快照（唯一详情/区间收益事实源）
- **用途**：规模/经理/公司/托管行/类型/评级 + 仓位 + 风险指标(1y/3y/5y) + **区间收益(ytd/1m/3m/6m/1y/3y/5y/年度)** + 排名
- **数据源**：**蛋卷基金 danjuanfunds**（akshare `fund_individual_*_xq` 实际请求 djapi）——fetch detail 任务（cron 每日 20:10）
- **关键字段**：`fund_code`, `trade_date`(快照日), `scale`, `fund_manager`, `fund_type`,
  `position_stock/bond/cash/other`, `sharpe_1y/3y/5y`, `max_drawdown_*`,
  `return_1m/3m/6m/1y/3y/5y/ytd`, `return_YYYY`(2015-2025), `rank_*`
- **消费方**：
  - `/fund/list` 排序/筛选（白名单：scale/return_ytd/sharpe_3y 等）
  - `/fund/{code}` 单只详情（myfund `ifund_detail`）
  - **myfund 自选/估算区间收益（2026-09-01 起直读 return_3m/6m/1y，唯一事实源）**
- ⚠️ `detail_json` 列存 `{}` 或 `{"source_unavailable":true}`（1164 只蛋卷不收录占位，7 天跳过/7 天后自动重探）；
  `scale` **允许为空**（8637 只蛋卷不返回，不触发重拉）

---

## 4. 持仓域

### fund_holdings（1,671,877 行）— 季报持仓
- **用途**：基金前十大股票/债券持仓（按 quarter）
- **关键字段**：`fund_code`, `quarter`(如 2025Q1), `holding_type`(stock/bond), `asset_code`, `asset_name`, `hold_ratio`, `hold_amount`, `hold_market_value`
- **消费方**：`/fund/{code}/holdings`、`fund_etf_linkage` 血缘、行业归因

---

## 5. 派生分析域

### fund_ai_analysis（5421 行）— AI 解读
- **用途**：LLM 生成的基金解读（verdict/评级/风格/标签/置信度）
- **关键字段**：`fund_code`, `verdict`, `rating`, `recommend`, `skill_score`, `luck_verdict`, `tags`, `confidence`, `model`, `analyzed_at`
- **消费方**：`/fund/list` 的 ai 字段、`_ai_public`

### fund_etf_linkage（1000 行）— 场外↔ETF 血缘
- **用途**：联接基金 ↔ 对应 ETF 映射（name_company_index 匹配）
- **关键字段**：`fund_code`, `etf_code`, `matched_by`, `confidence`

### fund_manager_tenure（86,210 行）— 经理任职史
- **用途**：每只基金历任经理任期/收益
- **关键字段**：`fund_code`, `seq`, `start_date`, `end_date`, `is_current`, `managers`, `tenure_days`, `tenure_return`

---

## 6. 任务/状态域

| 表 | 用途 | 关键字段 |
|---|---|---|
| `fetch_tasks`（40） | 拉取任务审计（类型/目标/成功/失败） | task_type, status, target_count, success_count, fail_count |
| `event_scan_status`（27408） | 分红/拆分扫描进度（pending/done） | fund_code, status, scanned_at |
| `nav_repair_queue`（29791） | 净值缺口修复队列（adj/event） | fund_code, task_kind, gap_start/end, status, attempts, next_retry_at |

---

## 7. 用户/业务域

| 表 | 用途 | 说明 |
|---|---|---|
| `users`（2） | 登录账号 | hashed_password(bcrypt) |
| `api_tokens`（9） | API 令牌（哈希存储） | token_hash, token_prefix, revoked |
| `app_settings`（0） | 键值配置 | key/value |
| `portfolios`（1） | 实盘组合 | name, cap |
| `user_holdings`（0） | 组合持仓 | fund_code, market_value, cost, base_shares |
| `holding_txns`（0） | 交易流水 | txn_type, amount, nav, shares |
| `perpetual_portfolio`（1） | 永续组合回测结果 | result_json |
| `query_presets`（5） | 用户查询预设 | filters_json |
| `fund_snapshots`（5） | 预设快照 | items_json, fund_count |

---

## 8. 防重复建设检查清单（新增需求先对照）

1. **要净值/复权净值/日涨幅** → 用 `fund_nav`（唯一），勿新建
2. **要区间收益（1m/3m/6m/1y/3y/5y/ytd/年度）** → 用 `fund_details.return_*`（蛋卷口径，唯一事实源）；缺失才允许本地自算兜底
3. **要规模/经理/公司/仓位/评级** → 用 `fund_details`
4. **要季报持仓** → 用 `fund_holdings`
5. **要经理任职史** → 用 `fund_manager_tenure`
6. **要基金主档/类型/拼音** → 用 `funds` + `fund_types`
7. **要分红拆分事件** → 用 `fund_div_split`
8. **要交易日历** → 用 `trade_dates`
9. **跨服务**：myfund 侧**禁止**建基金净值/详情/持仓表，一律走 iFund API 读（唯一源在 iFund）；
   fin-data 侧重在 A 股行情/资金流，基金数据以 iFund 为准（fund-navi 类需求引 iFund）。

## 9. 已知注意点

- `fund_details.detail_json` 未存原始响应（`{}` 或 source_unavailable 占位）——如需原始响应可后续启用，勿另建表
- `fund_nav` 3394 万行 → 定期 `checkpoint_wal` + 增量维护，勿全量重建
- `fund_ai_analysis` 仅 5421 行（约 20% 覆盖率）——AI 解读按需补充，非全量
- 净值域与详情域 trade_date 含义不同：`fund_nav.trade_date` 是净值日；`fund_details.trade_date` 是蛋卷快照日（可能滞后，如 07-31）
