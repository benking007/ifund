# iFund 数据结构与内置拉取功能盘点（2026-09-01，MySQL 迁移后）

> 数据源：生产 `MySQL 192.168.0.9/ifund`（`DB_BACKEND=mysql`，凭据 `/etc/ifund-prod.env`）；
> 旧 SQLite `backend/data.db`（9.2GB，4161 万行）已全量迁移并保留作回滚。文档对应 ifund 后端现状。

## 一、表结构总览（24 张表）

### 1. 基础主数据（4 表）
| 表 | 行数 | 说明 | 关键列 |
|---|---|---|---|
| `funds` | 27408 | 基金主表（全市场） | code, name, type, fund_type, pinyin_abbr, pinyin_full |
| `fund_types` | 33 | 基金类型字典 | type, 描述 |
| `trade_dates` | 8797 | 交易日历 | id, trade_date |
| `stock_industry` | 5927 | 股票申万行业映射 | stock_code, stock_name, market, sw_l1/l2/l3, em_industry |

### 2. 行情/净值（3 表）
| 表 | 行数 | 说明 | 关键列 |
|---|---|---|---|
| `fund_nav` | 3394.8万 | 日净值（主力） | fund_code, trade_date, nav, acc_nav, daily_return, adj_nav, fetch_time |
| `fund_cum_return` | 577.3万 | 累计收益序列 | fund_code, trade_date, cum_return |
| `fund_div_split` | 5 | 分红拆分事件 | — |

### 3. 基金详情/持仓（2 表）
| 表 | 行数 | 说明 | 关键列 |
|---|---|---|---|
| `fund_details` | 27408 | 每基金一行快照（蛋卷口径） | fund_code, trade_date, fetch_time, scale, return_3m/6m/1y, 年度收益 2015-2025, detail_json |
| `fund_holdings` | 167.2万 | 季报持仓明细 | fund_code, quarter, holding_type, asset_code, asset_name, hold_ratio |

### 4. 派生/分析（3 表）
| 表 | 行数 | 说明 | 关键列 |
|---|---|---|---|
| `fund_ai_analysis` | 5421 | AI 定性分析（本地 LLM） | fund_code, manager, verdict, rating, recommend, skill_score, luck_verdict |
| `fund_etf_linkage` | 1000 | 场外基金↔ETF 映射 | fund_code, fund_name, etf_code, etf_name, matched_by, confidence |
| `fund_manager_tenure` | 86210 | 基金经理任职史（东财 F10） | fund_code, seq, start_date, end_date, is_current, managers, tenure_text |

### 5. 任务/队列（3 表）
| 表 | 行数 | 说明 |
|---|---|---|
| `fetch_tasks` | 40 | 拉取任务统计（target/success/fail/current） |
| `event_scan_status` | 27408 | 净值事件扫描状态（每基金一行） |
| `nav_repair_queue` | 29791 | 净值缺口修复队列（gap_start/gap_end/status/attempts/next_retry_at） |

### 6. 用户/组合/预设（6 表）
| 表 | 行数 | 说明 |
|---|---|---|
| `users` | 2 | 用户 |
| `api_tokens` | 9 | API 令牌 |
| `app_settings` | 0 | 全局设置（空） |
| `portfolios` / `user_holdings` / `holding_txns` | 1/0/0 | 组合/持仓/交易（未启用） |
| `perpetual_portfolio` | 1 | 永续组合 |
| `query_presets` / `fund_snapshots` | 5/5 | 查询预设/镜像快照 |

### 7. fund_details 数据形态（2026-09-01）
- trade_date 分布：08-31=27197 / 07-31=203 / 08-28=8
- `detail_json` 全为 `{}` 或 `{"source_unavailable":true}`（占位），未存原始 JSON
- **source_unavailable 占位：1164 只**（蛋卷不收录：后端份额/定期开放债/部分 FOF 联接；7 天内跳过拉取，7 天后自动重探）
- scale 缺失：8637 只（蛋卷不返回规模）——**已放宽 is_expired 判据，允许为空，不再重拉**

## 二、内置数据拉取功能

### 1. CLI 命令树（`ifund_cli.py`，直连 data.db 免认证）

顶层命令：`preset / fetch / nav / analyze / ai-analyze / historical / perpetual / holdings`

`ifund fetch` 六个数据拉取子命令：

| 子命令 | 数据源 | 参数 | 写入表 |
|---|---|---|---|
| `fetch calendar` | 交易所日历 | --json/--user | trade_dates |
| `fetch industry` | 申万/东财 | --mode {sw,em} --codes | stock_industry |
| `fetch detail` | **蛋卷 danjuanfunds djapi**（akshare `fund_individual_*_xq`，阿里云香港 47.75.232.147） | --codes/--types，is_expired 去重 | fund_details |
| `fetch holdings` | 季报（东财） | --codes/--types/--incremental（只拉缺上季持仓的非货币） | fund_holdings |
| `fetch nav` | **Tushare + akshare**（`fund_individual_fund_info` 等；Tushare 限频约 1 次/小时） | --codes/--types | fund_nav |
| `fetch manager` | **东财 F10** | --codes/--types | fund_manager_tenure |

### 2. worker 并发机制（`app/common/worker_base.py`）
- 默认并发 4，`IFUND_WORKER_CONCURRENCY` 环境变量可调（cron 用 8，detail cron 用 6）
- `process_one` 内部自带幂等/增量（is_expired 判据），重复执行不破坏断点续跑
- ThreadPool 或 ProcessPool（按可 pickle 性自动选）

### 3. 调度脚本（`scripts/`）
| 脚本 | 用途 |
|---|---|
| `daily_nav_sync.py` | 净值增量同步（P1 货币/债券 → P2 股票/混合 → P3 QDII/FOF 分组，可 --round morning/noon、--type-priority） |
| `quarterly_holdings_sync.sh` | 季报持仓增量同步（窗口门控） |
| `nav_repair_pass.py` / `run_nav_gov_full.sh` / `run_nav_events_adj.sh` | 净值治理（缺口修复/事件复权） |
| `mark_source_unavailable.py` | 蛋卷不收录基金占位标记（2026-09-01 新增） |
| `backfill_etf_linkage.py` | ETF 映射回填 |
| `probe_tushare.py` / `data_selfcheck.py` | 探测/自检 |

### 4. crontab 自动任务（ifund 相关）
> 全部任务均注入 `/etc/ifund-prod.env`（`set -a; . /etc/ifund-prod.env; set +a`）——MySQL 迁移后必选，否则子进程连不上库。

| 时间 | 任务 | 说明 |
|---|---|---|
| 周一至五 20:00 | `daily_nav_sync.py`（round=主） | 净值增量 |
| 周二至六 08:00 | `daily_nav_sync.py --round morning` | 早盘净值 |
| 周二至六 12:00 | `daily_nav_sync.py --round noon` | 午间净值 |
| 每天 08:00 | `quarterly_holdings_sync.sh` | 季报持仓（窗口门控，脚本内加载 env） |
| **每天 20:10** | `ifund_cli.py fetch detail`（2026-09-01 新增） | fund_details 区间收益刷新，is_expired 去重 |

### 5. AI 分析
- `ifund ai-analyze`（CLI）：本地 LLM 生成 verdict/rating/skill_score → fund_ai_analysis（当前 5421/27408 = 19.8% 覆盖率，可批量补）
- `ifund nav`：净值治理（缺口修复 nav_repair_queue 驱动）
- `ifund analyze`：组合分析（预设→仓位建议→穿透/赛道/表现）

## 三、数据流图

```
外部源                    拉取层                   存储层                   消费层
蛋卷 djapi ──┐
Tushare ─────┼→ fetch detail/nav/holdings/manager ─→ MySQL ifund 库            ─→ myfund (HTTP API)
东财 F10 ────┤     (worker_base 并发 + is_expired 去重)    fund_details/fund_nav/   (估值/区间收益/自选)
东财季报 ────┘                                      fund_holdings/
                                                     fund_manager_tenure
   │
   └→ daily_nav_sync.py (cron 20:00/08:00/12:00) → fund_nav 增量
   └→ quarterly_holdings_sync.sh (cron 08:00)     → fund_holdings
   └→ fetch detail (cron 20:10)                   → fund_details 区间收益

本地 LLM ──→ ai-analyze → fund_ai_analysis
```

## 四、已知遗留问题
1. **fund_ai_analysis 覆盖率 19.8%**：需要时用 `ifund ai-analyze batch` 补
2. **Tushare 限频**：nav 拉取约 1 次/小时（hk 相关无权限），大范围补数慢，靠 nav_repair_queue 排队
3. **fund_details trade_date 滞后**：07-31 有 203 只（蛋卷源快照滞后），非 bug
4. ~~scale 缺失重拉~~ **已解决**（2026-09-01 放宽判据，允许为空）

## 五、MySQL 迁移记录（2026-09-01）
- 源：SQLite `data.db` 9.2GB / 24 表 / **4161 万行**（fund_nav 3394 万 + cum_return 577 万 + 其余 18.5 万）
- 目标：`192.168.0.9/ifund`（与 myfund 同服务器），账号 `ifund`（最小权限 `ifund.*`）
- 工具：`scripts/migrate_sqlite_to_mysql.py`（分片游标 + checkpoint 断点续跑 + 每表对账），耗时 39 分钟
- 后端：`app/db/mysql.py`（PyMySQL；DDL 由 `_convert_schema_sql` 从 schema_sqlite.sql 实时转换，
  UNIQUE 约束列 VARCHAR(64) 防 3072 字节超限，保留字列名加反引号）
- 切换：`DB_BACKEND=mysql`（/etc/ifund-prod.env）+ systemd 双 EnvironmentFile + cron `set -a` 注入
- 回滚：改回 `DB_BACKEND=sqlite` 即用旧 data.db（已保留 ≥7 天）
