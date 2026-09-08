# iFund 每日净值同步方案重设计（v2）

> 状态：设计稿，待评审。目标是**整体替换**现有 `scripts/daily_nav_sync.py` + `app/fund_nav/fetch/*` 的每日增量链路。
> 触发背景：2026-09-01 夜间轮因连接泄漏（`[Errno 24] Too many open files`）导致 25483 只基金里 19404 只采集失败。

---

## 1. 现状与问题

### 1.1 现状数据流

```
cron 三时段（20:00 night / 08:00 morning / 12:00 noon）
  └─ daily_nav_sync.py
       ├─ 优先级分组 P1(货/债) → P2(股票/混合) → P3(QDII 海外)
       ├─ 夜间内部：每 5 分钟轮询到 23:00，失败按 5/10 分钟退避
       ├─ 每 50 只基金切一个 chunk，每个 chunk 新建 ThreadPoolExecutor(8)
       └─ 单只处理 worker._process_one：
            ├─ 东财 F10 增量（主源，一个接口含 nav/acc_nav/daily_return/cum_return）
            ├─ 失败 → akshare 全量（降级 1，3 个 indicator 各打一次）
            ├─ 再失败 → JS 正则兜底（降级 2）
            └─ 成功 → adj_engine.maintain_adj（tushare 前复权维护）
       └─ 落库 fund_nav / fund_cum_return（MySQL，thread-local 连接）
       └─ 失败 → nav_repair_queue → nav_repair_pass 重试
```

### 1.2 问题清单

| # | 问题 | 严重度 | 后果 |
|---|------|--------|------|
| 1 | **连接泄漏**：`mysql.py` 用 thread-local + `_connections` 列表持有连接，不随线程退出释放；`worker.py` 的 `_RequestsProxy` 也是 thread-local `requests.Session` 不关闭；`process_codes` 每个 chunk 新建 `ThreadPoolExecutor(8)` | P0 | 处理约 6000 只后 fd 耗尽，9-01 夜间轮 19404/25483 失败 |
| 2 | 同步模型过复杂：三层降级 + 前复权 + 事件维护，单只基金最多 5+ 个 HTTP 请求 | 高 | 维护成本高、隐性 bug 多 |
| 3 | 夜间「5 分钟轮询 + 退避」本质是「拉不到就反复拉」 | 高 | 系统性故障时雪崩（越重试越失败） |
| 4 | 全量扫描 2.7 万只基金、逐只判断 skip | 中 | 每天白扫 2.7 万行 |
| 5 | 前复权（tushare）与单位净值增量耦合，每只每日维护 | 中 | tushare 限频吃紧、拖慢主链路 |
| 6 | 无整体进度状态，进程中断后需重扫全量 | 中 | 断点续跑弱 |

---

## 2. 设计目标

1. **连接安全**：进程级连接复用，无泄漏，消灭 fd 耗尽类事故。
2. **简单**：单一主源、单一降级路径，逻辑可读可测。
3. **可靠**：幂等 + 断点续跑 + 失败可重试可查。
4. **高效**：只拉「需要增量」的基金，异步并发 + 限速。
5. **可观测**：每轮、每只基金的状态可查。

---

## 3. 新架构

### 3.1 组件

```
NavSync 进程（单进程，asyncio 单事件循环）
│
├─ 连接层（进程级单例，全程复用）
│   ├─ HTTP：httpx.AsyncClient
│   │     ├─ eastmoney_client：东财 F10，连接池 max_keepalive≈10，全局限速器
│   │     └─ tushare_client：前复权专用，独立限频（低频任务用）
│   └─ DB：进程级小连接池（固定 2~4 连接，或单连接 + 批量 upsert）
│
├─ 调度层（简化：三时段各跑一次，去掉夜间轮询）
│   ├─ night  20:00：当日收盘后首次增量
│   ├─ morning 08:00：补前一交易日遗漏
│   └─ noon   12:00：兜底
│
├─ 执行层（asyncio 并发）
│   ├─ 增量目标 = SQL 筛选 stored_latest < base_trade_date 的基金（含新基金）
│   ├─ 并发度 = 可配（默认 8~16），asyncio.Semaphore 限流
│   ├─ 单只：fetch 增量（东财 F10）→ 批量 upsert → 推进 stored_latest
│   └─ 失败 → repair 队列（nav_repair_queue）
│
├─ 重试层（repair loop，独立轻量）
│   └─ 每时段末尾 + 独立低频循环，拉 repair 队列重试（指数退避 + 连接复用）
│
└─ 状态/可观测
    ├─ fund_nav.stored_latest：每只基金推进点（已有）
    ├─ nav_repair_queue：失败重试（已有）
    └─ sync_run：每轮统计（新增，可选）
```

### 3.2 数据流（每日增量）

```
1. 判定交易日 + 计算 base_trade_date（当天未发布则取 T-1）
2. SQL 直接筛出「需要增量」的基金集合：
     SELECT f.code FROM funds f
     LEFT JOIN (每只基金 max(trade_date)) nav ON ...
     WHERE 最新净值日 < base_trade_date OR 无净值记录
3. asyncio 并发处理（Semaphore 限流 + 全局限速器）：
     for code in targets:
       rows = eastmoney.fetch_nav_incremental(code, stored_latest, base)
       upsert(rows)                 # 幂等
       推进 stored_latest
4. 失败基金 → nav_repair_queue（带 gap 区间 + 错误原因 + 尝试次数）
5. repair loop 重试失败队列（退避）
```

---

## 4. 关键设计决策

### 4.1 连接管理：异步 + 进程级复用（核心，消灭 P0）

- **DB**：废除 thread-local 模式。改为进程级连接池（固定 `N=2~4` 个 `pymysql` 连接，或单连接串行写 + 批量 `executemany`）。连接在进程生命周期内复用，进程退出时统一 `close()`。绝不在并发 worker 里逐个新建连接。
- **HTTP**：废除 `requests` + thread-local Session。改用 `httpx.AsyncClient` 进程级单例，`max_keepalive_connections` 设小值（如 10），连接池复用。
- **并发**：废除「每 chunk 新建 ThreadPoolExecutor」。改用 asyncio 单事件循环 + `asyncio.Semaphore(concurrency)`，全程一个并发域。
- **限速**：保留对东财的节流（全局令牌桶/最小间隔，默认 0.3s/请求），避免并发时打爆上游。

### 4.2 数据源收敛：东财 F10 单一主源

- 每日增量**只用东财 F10 `/f10/lsjz`**（一个接口返回 `nav/acc_nav/daily_return/cum_return`，无需 akshare 三个 indicator + JS 兜底）。
- akshare 全量 / JS 正则降级**不进每日主链路**，降级为独立的「历史回补工具」（`ifund nav backfill`），仅在：① 新基金首次入库；② 东财 F10 对某基金持续解析异常时，手动触发。
- 降级路径从「三层自动级联」收敛为「单层 + 失败入 repair」，行为可预期。

### 4.3 增量目标筛选：SQL 而非全量扫描

- 不再 `load_fund_rows()` 拉全量 2.7 万行逐只判断。
- 用一条 SQL 直接筛出「最新净值日 < base_trade_date」的基金（含从未有净值记录的新基金），只对这批拉增量。
- 效果：正常交易日，只有「当天实际发布净值」的基金才进执行集合；QDII/FOF 等 T+2 披露的基金自然落到 base_trade_date 之前，不会空拉。

### 4.4 失败重试：repair 队列驱动（去掉夜间轮询）

- 删除 `run_night` 的「每 5 分钟轮询 + 退避」状态机。
- 失败统一入 `nav_repair_queue`（已有表），记录 `gap_start/gap_end/error/attempts`。
- 重试由两处驱动：
  1. 每时段（night/morning/noon）执行完增量后，跑一遍 `repair_pass`（重试队列）。
  2. 一个独立的低频 repair 循环（如每 30 分钟，只在工作日披露窗口内），带指数退避。
- 好处：重试与主增量解耦，系统性故障时不会「反复空拉雪崩」。

### 4.5 调度简化

- cron 三时段保持不变（20:00 / 08:00 / 12:00），但脚本内部逻辑简化为「一次增量扫描 + 一次 repair」。
- 三时段的意义从「夜间多轮抓发布进度」改为「三次补漏机会」，靠 repair 队列兜底，而非轮询。

### 4.6 前复权解耦

- `adj_nav`（前复权，tushare）与单位净值增量**解耦**，拆成独立低频任务：
  - 只对「需要 fq 口径」的基金集合（自选、持仓、重点池）维护，而非每日全量 2.7 万只。
  - 独立调度（如每日 1 次、或按需触发），独立连接与限频，失败入独立 repair 队列。
- 单位净值增量链路不再调用 `maintain_adj`，主链路更轻、更稳定。

### 4.7 可观测性

- `sync_run` 表记录每轮：轮次、时段、目标数、成功/跳过/失败、耗时、开始/结束时间。
- 结构化日志保留 `[night] 类型 待拉 X 成功 Y 失败 Z 耗时` 的聚合行（对齐现有习惯）。
- `nav_repair_queue` 增加可查询的失败明细（code + error + attempts + 最近时间）。

---

## 5. 数据模型

- `fund_nav` / `fund_cum_return`：不变，继续用 `INSERT ... ON DUPLICATE KEY UPDATE`（幂等）作为落地目标。
- `nav_repair_queue`：沿用现有表，确认字段含 `fund_code/task_kind/gap_start/gap_end/error/attempts`。
- 新增 `sync_run`（可选）：记录每轮统计，便于排查「哪天哪轮拉了哪些」。

---

## 6. 落地计划（分阶段）

1. **阶段 0：止血**（不依赖重写）
   - 临时用低并发单只回补，把 9-01 及之前失败基金的净值补上（`nav backfill --codes ...` 或全量 pending）。
   - 注意：先修连接泄漏再补，否则补数也会再爆 fd。

2. **阶段 1：核心重写**
   - 新建 `scripts/nav_sync.py`（或复用同名）：
     - asyncio + httpx.AsyncClient + 进程级 DB 连接池
     - SQL 增量目标筛选 + Semaphore 并发 + 限速
     - 失败入 repair
   - 新建 `repair` 独立循环。
   - 前复权拆出独立任务。
   - 保留 cron 三时段触发，替换旧脚本。

3. **阶段 2：验证**
   - 用 `--once` 冒烟 + 小集合（自选基金）先跑，核对增量数据与东财官方一致。
   - 观察 fd（`/proc/<pid>/fd` 计数）确认无泄漏。
   - 全量增量与现有库对账（stored_latest 是否推进、无重复无缺失）。

4. **阶段 3：切换与下线**
   - 灰度：新脚本与旧脚本并行跑 1~2 天对账。
   - 下线旧 `daily_nav_sync.py` 的夜间轮询状态机与三层降级。

---

## 7. 待确认

- 东财 F10 的稳定限频上限（现有 0.3s/请求 + 并发 8 约 30 QPS，新方案是否沿用该节奏）。
- 前复权「需要 fq 口径」的基金集合如何界定（自选 + 持仓 + 重点池？是否有独立标记）。
- 是否保留「夜间 20:00 后禁止大 batch（time gate）」这类治理约束，以及新约束口径。
