# 公募基金数据同步机制全面 Review 与重构方案

> 评审日期：2026-09-02（Asia/Shanghai）
> 评审范围：/root/workspace/ifund/backend 下基金目录、交易日历、净值、复权/事件、持仓、详情、经理、ETF 关联、股票行业及其调度、质量检查脚本。
> 评审性质：只读审查与方案设计。本次未修改业务代码、未提交代码、未运行抓取或写库脚本。

## 1. 结论摘要

当前系统的首要矛盾不是某个数据源解析器，而是“没有资源边界的并发执行”叠加“没有统一任务状态的多入口同步”。2026-09-01 夜间已发生确定性的 P0 事故：MySQL 连接和 HTTP 会话在批次/线程结束后没有归还，净值脚本又为每 50 只基金重新创建 8 线程线程池；约处理 6000 只基金后文件描述符耗尽，最终 25483 只目标中 19404 只失败。

建议将该域重构成一个受控的同步平台，而不是继续堆叠单脚本降级逻辑：

```text
调度/租约
    -> 目标规划与水位
    -> 数据源适配器（统一限速、重试、熔断、指标）
    -> 标准化与质量校验
    -> 幂等、原子落库（数据与水位同事务）
    -> 失败队列 / 死信队列
    -> 自检、指标、告警、dashboard
```

核心决策如下：

1. 先消除无界资源：MySQL 使用进程级、有上限、可归还的连接池；HTTP 使用进程级复用客户端；一个任务进程只维护一个有界执行域；连接和客户端在 shutdown 时显式关闭。
2. 每日净值只负责“可解释的增量事实写入”。复权、分红拆分事件、全量历史回补从主链路剥离，不能由每只基金的成功净值请求隐式触发。
3. 增量依据由“扫全量基金后逐只查 MAX(trade_date)”升级为 SQL 分页目标 + 持久化水位。当前表中没有设计稿所称的 fund_nav.stored_latest 字段，不能按该假设实施；应新增状态表，或在过渡期从索引化聚合查询得到水位。
4. “没有返回数据”不等于“同步成功”。必须区分 success_with_data、success_no_data、skipped_fresh、rate_limited、timeout、malformed、permanent_unavailable 等状态。
5. 以一个任务清单/调度清单作为唯一事实来源，淘汰 holdings_batch*.sh、宽泛的 run_nav_*.sh 和隐藏在业务 API 中的同步分支。旧入口只保留一个发布周期的兼容壳。

### 1.1 现状判断中的几个重要修正

docs/nav-sync-redesign.md 的方向基本正确，但不能原样作为实施规格：

- 设计稿把 fund_nav.stored_latest 写成“已有”，实际 schema_sqlite.sql:154-172 的 fund_nav 只有逐日事实和唯一键，没有该字段；app/fund_nav/crud/nav_crud.py:28-31 的 stored_latest 是运行时查询 MAX(trade_date)。
- 设计稿建议直接切 asyncio + httpx，但 AkShare 当前仍是同步调用且存在模块级 requests 替换。第一阶段应先实现“有界同步执行器 + 进程级客户端/连接池”，待适配器真正异步化后再引入事件循环，避免把阻塞 SDK 套进伪异步模型。
- 设计稿主要覆盖净值，未解决详情、持仓、经理、行业、日历的全量替换/非原子写入、SQLite 辅助脚本、任务状态不一致和质量检查不接生产库等横向问题。

## 2. 审查方法与运行边界

本次检查了：

- 根用户 crontab、systemctl list-timers --all、ifund.service；
- scripts/、cli/、app/fund_*、app/trade_calendar、app/stock_industry 及数据库抽象层；
- schema_sqlite.sql、数据字典、已有净值重设计稿；
- logs/daily_nav_cron.log、详情/持仓/事件复权日志的只读内容。

“未发现调度”仅表示在根 crontab 和 systemd timer 中未发现；bgjob、人工启动、其他主机上的调度若存在，需要在实施阶段通过主机和部署清单再核对。当前观察到的 systemd 只有长期运行的 Web 服务 ifund.service，未发现 iFund 同步 timer。

## 3. 同步任务清单

下表按“数据事实同步、派生维护、质量/运维”完整列出当前已发现的入口。调度栏区分“已观察的自动调度”和“代码存在但未观察到自动调度”，避免把手工脚本误当成生产定时任务。

| 任务 / 当前入口 | 数据源 | 调度 / 触发方式 | 落库表 | 当前增量 / 全量策略与主要风险 |
|---|---|---|---|---|
| 基金目录：app/fund/fetch/fetcher.py、POST /api/fund/sync | AkShare fund_name_em()（东财目录） | API 人工触发；根 crontab 未发现定时 | funds、fund_types | 全量快照；fund_crud.replace_all() 是 DELETE 后插入，未在一个事务内完成，短时可能出现空目录或目录/分类不一致。 |
| 交易日历：cli fetch calendar、POST /api/trade_calendar/sync | AkShare tool_trade_date_hist_sina | CLI/API 人工触发；根 crontab 未发现定时 | trade_dates、fetch_tasks | 全量替换；base_trade_date() 依赖该表，源失败时没有可靠的“日历新鲜度”闸门和告警。 |
| 每日净值：scripts/daily_nav_sync.py、app/fund_nav/fetch/worker.py | 主源东财 F10 lsjz；降级 AkShare 三类接口；再降级东财 JS；复权还会调用 Tushare | 根 crontab：工作日 20:00；Tue-Sat 08:00 --round morning；Tue-Sat 12:00 --round noon（crontab 第 50-52 行） | fund_nav、fund_cum_return；间接触发 fund_div_split、event_scan_status、adj_nav | 先把全量基金拉入内存，再按每只基金 MAX(trade_date) 判断；夜间 20:00-23:00 内部每 5/10 分钟重扫。每 50 只创建一个新线程池；东财失败会自动级联 AkShare 全量和 JS，成功后还维护复权。唯一键/upsert 有幂等基础，但无持久化任务水位和清晰的 no-data 状态。 |
| 净值历史回补：app/fund_nav/fetch/backfill_worker.py、ifund_cli.py nav backfill | 东财 F10 全量接口 | 手工；scripts/run_nav_gov_full.sh 阶段 1；未发现根 crontab 定时 | fund_nav、nav_repair_queue | 按代码拉 2000-01-01 至今天的全量历史；工作日 20:00-23:00 有 batch=50 约束和 0.3 秒间隔。仍使用线程池/线程局部 DB，会与日常任务争夺资源。 |
| 净值失败修复：scripts/nav_repair_pass.py | 依据任务类型调用净值回补或复权 | 代码注释建议 21:30，但根 crontab 未发现；也由每日夜间轮末尾调用 | nav_repair_queue、目标事实表 | 队列可重试并有退避，但 attempts>=3 目前只是告警，未形成明确 dead-letter/人工接管状态；并发和任务租约没有统一。 |
| 基金详情：cli fetch detail、详情 API worker | AkShare 的详情接口；脚本/注释称“雪球”，数据清单称蛋卷/东财口径，来源命名需核实 | 根 crontab 每日 20:10（ifund_cli.py fetch detail，crontab 第 66 行）；也被治理脚本阶段 5 调用 | fund_details | 以 7 天或日期 freshness 跳过；一次调用 basic/holdings/analysis/achievement，多接口部分成功也可能写入。失败不进入统一 repair 队列；detail_crud.upsert 为读后更新/插入。 |
| 季度持仓：scripts/quarterly_holdings_sync.sh、cli fetch holdings --incremental | AkShare 东财组合/债券持仓接口 | 根 crontab 每日 08:00；脚本只在 1/4/7/10 月 20-31 日窗口真正抓取；holdings_batch.sh、holdings_batch2/3/4.sh 手工补拉 | fund_holdings | 以最近已存季度与上一披露季度比较；单只基金会抓两年范围内的股票/债券数据，约四类请求。写入时删除触及的季度/类型再插入；空响应可被视为成功但无覆盖证据。批处理脚本硬编码旧 SQLite data.db，与生产 MySQL 路径不一致。 |
| 基金经理：cli fetch manager、app/fund_manager/api/router.py | 东财 F10 fundf10.eastmoney.com/jjjl_CODE.html | API/CLI 人工触发；根 crontab 未发现定时 | fund_manager_tenure | 按自然日 freshness；每次成功先删除该基金全部任职史再插入，删除和插入不在同一显式事务中；网络无重试、无统一限速、无 repair 记录。 |
| 分红/拆分事件：cli nav events、run_nav_events_adj.sh、events_worker.py | Tushare fund_div；fund_split 探测失败后禁用 | 手工治理脚本；每日净值成功路径还会调用 maintain_adj() 间接触发单基金事件同步 | fund_div_split、event_scan_status、当前误用的 nav_repair_queue | 支持全量或指定代码；有进程内最小间隔和指数退避，但多个进程/脚本之间不共享限速；事件失败被记成 task_kind='adj'，故障域混淆。 |
| 前复权净值：cli nav adj、run_nav_events_adj.sh、run_nav_gov_full.sh、adj_engine.py | Tushare fund_nav 的复权字段；本地分红事件计算兜底 | 手工治理脚本；每日净值成功后逐基金触发；未发现独立可靠 timer | fund_nav.adj_nav、fund_nav.adj_src、nav_repair_queue | 治理脚本按全库 2.7 万只调用；主链路与单位净值耦合。事件计算对历史净值和事件存在朴素的逐行/逐事件计算，失败仍回到共享修复队列。 |
| ETF 关联：scripts/backfill_etf_linkage.py、ETF linkage API | 本地 funds、fund_details，无外部抓取 | 手工回补；根 crontab 未发现 | fund_etf_linkage | 全量从本地重算；匹配和删除在事务中处理，但脚本运行时再次 init_db，职责不应由回补脚本承担。 |
| 股票行业：app/stock_industry/fetch/sw_worker.py、em_worker.py、industry API | 申万/乐估估、东财、CNInfo 兜底 | API/CLI 人工触发；根 crontab 未发现 | stock_industry、fetch_tasks | 申万按已有三级行业覆盖增量，东财补持仓未覆盖股票；申万串行且每次约 2 秒，东财有约 0.6 秒间隔。单股票事务，任务状态使用 finished，与其他 worker 的 completed 不一致。 |
| 标记详情源不可用：scripts/mark_source_unavailable.py | 无外部源；人工确认 | 手工、可 dry-run；未发现定时 | fund_details | 写入 source_unavailable 占位，避免每日空拉；应改为显式状态/来源表，不能把业务缺失和抓取成功混在详情事实中。 |
| 数据自检：scripts/data_selfcheck.py | 无外部源 | 手工；未发现根 crontab | 无写入（预期） | 只读旧 SQLite immutable 快照，检查资金、详情、持仓、净值等；不是生产 MySQL 质量门禁，净值检查窗口硬编码为 2026-07-15，--json 后仍混出人类可读问题文本。 |
| Tushare 探测：scripts/probe_tushare.py | Tushare fund_div | 仅被事件治理脚本循环调用 | 无写入 | 单次探测本应只发一个请求，但 __main__ 中调用 main() 两次；无 raise_for_status，不能作为可靠源健康探针。 |

### 3.1 目前可见的调度关系

```text
20:00 工作日 ─┐
08:00 Tue-Sat ├─ daily_nav_sync：一次外层调用，但 night 内部还会循环重扫
12:00 Tue-Sat ┘

08:00 每日 ───── quarterly_holdings_sync.sh
                 └─ 仅季度披露窗口实际执行

20:10 每日 ───── ifund_cli.py fetch detail

人工/bgjob ───── nav_repair_pass、nav backfill、events、adj、gov_full、ETF linkage、行业、自检
```

## 4. 问题清单（按严重度排序）

### P0：必须先解决，否则所有回补和重构验证都不可靠

| 编号 | 问题 | 文件:行号证据 | 影响 / 判断 |
|---|---|---|---|
| P0-1 | MySQL 连接没有“借出—归还”生命周期，_connections 只是不断增长的引用列表。每个线程首次使用都创建连接，只有进程退出/显式 close() 才统一关闭；线程池销毁并不会触发业务释放。 | app/db/mysql.py:573-628；其中 _new_connection 追加到列表见 589-594，close 只关闭列表见 619-628。 | 这是确定的 FD/连接泄漏。长期 Waitress 进程和短命同步线程都会受影响，不能靠增加 ulimit 或降低单次 batch 治本。 |
| P0-2 | 每 50 只基金重新创建一个 ThreadPoolExecutor，且根 crontab 将并发设为 8；夜间还重复整轮扫描。 | scripts/daily_nav_sync.py:475-485、537-542、567-613；根 crontab 第 50-52 行。 | 线程、DB 连接、HTTP 会话和上游请求量按 chunk 放大；与 P0-1 叠加后耗尽文件描述符。 |
| P0-3 | 2026-09-01 事故已有日志证据：大量连接失败为 Errno 24，夜间轮最终 19404/25483 失败。 | logs/daily_nav_cron.log:1811055、1811101、1811379-1811381；全文件检索 Too many open files 计数为 59157。 | 已经是生产可见的数据完整性事故；在连接层修复并建立资源回归测试前，不应继续运行大规模回补。 |
| P0-4 | 生产事实库与自检目标脱节。自检硬编码 /root/workspace/ifund/backend/data.db 并以 immutable SQLite 打开；Web /api/stats 也直接打开 SQLite 快照。 | scripts/data_selfcheck.py:16、35-37；app/main.py:94-105；docs/data-structure-inventory.md:1-4。 | 自检可能对旧快照报“健康”，无法作为 MySQL 同步成功/失败的判断依据，告警闭环实际上是断的。 |

### P1：影响正确性、可恢复性、上游稳定性和运维判断

| 编号 | 问题 | 文件:行号证据 | 影响 / 判断 |
|---|---|---|---|
| P1-1 | 每日净值目标是全量 Python 扫描，而不是按水位的 SQL 目标集；单只成功后马上调用复权/事件维护。 | scripts/daily_nav_sync.py:359-380、447-472、537-549；app/fund_nav/fetch/adj_engine.py:311-323。 | 即使大多数基金已是最新，仍付出全量扫描、逐只查询和衍生任务成本；正常同步、复权、事件失败互相放大。 |
| P1-2 | 自动降级和重试没有按错误类别治理。东财失败会回退 AkShare 全量，再回退 JS；每类异常都可能重试，夜间再重复整轮。 | app/fund_nav/fetch/worker.py:71-88、185-242；scripts/daily_nav_sync.py:567-613。 | 上游限流、格式错误、认证失败和临时超时被当成同一种失败，容易形成“越失败越重试”的风暴；数据源责任边界不清。 |
| P1-3 | HTTP 复用不统一：AkShare 的 _RequestsProxy 为每个线程创建 Session 且不关闭；持仓 worker 采用同样模式；东财、JS、经理、Tushare 客户端还大量直接 requests.get/post。 | app/fund_nav/fetch/worker.py:41-68；app/fund_holdings/fetch/worker.py:41-67；app/fund_nav/fetch/eastmoney.py:79-123；app/fund_manager/fetch/worker.py:76-84；app/fund_nav/fetch/tushare_client.py:66-89。 | 连接池无法跨 worker 复用，Session 生命周期不可观测；不同源各自超时/重试/连接策略，导致 FD、上游连接和故障处理不一致。 |
| P1-4 | 限速器是局部实现，不是跨任务、跨进程的源策略。事件 worker 仅有进程级 limiter，东财增量适配器自身没有统一限速和重试。 | app/fund_nav/fetch/events_worker.py:50-93；app/fund_nav/fetch/eastmoney.py:79-123；app/common/rate_limit.py:1-88（这是 API 入站 IP 限流，不是上游限流）。 | 日净值、回补、详情、持仓、治理脚本可能同时打同一主机；单个脚本“礼貌”不代表系统整体礼貌。 |
| P1-5 | 增量状态不持久化为同步水位。现有 stored_latest 每次从逐日事实表计算，设计稿引用的 fund_nav.stored_latest 实际不存在。 | app/fund_nav/crud/nav_crud.py:28-31；schema_sqlite.sql:154-172；docs/nav-sync-redesign.md:69-80。 | 进程中断后只能重扫；无法区分“源已确认无数据”“该基金发布滞后”“本次提交未完成”；目标规划和恢复都不具备稳定的 run/item 语义。 |
| P1-6 | 任务审计不完整且状态枚举不统一。通用 worker 更新 fetch_tasks，但 cli/fetch.py 的 per-fund 路径不创建任务；行业 worker 使用 finished，通用 worker 使用 completed/terminated。 | app/common/worker_base.py:197-267；cli/fetch.py:31-68；app/stock_industry/fetch/sw_worker.py:84-125；schema_sqlite.sql:108-124。 | dashboard 无法回答一次 run 覆盖了哪些基金、哪一个源失败、提交了多少行；“无任务记录”与“成功”难以区分。 |
| P1-7 | 空结果可能被当作成功。净值和持仓 worker 在源返回空 rows 时仍可能返回成功；详情接口对多个字段采取静默 []/None 降级。 | app/fund_nav/fetch/worker.py:205-242；app/fund_holdings/fetch/worker.py:168-180；app/fund_detail/fetch/worker.py:30-53。 | 上游页面变化、限流、基金确实未发布三种情况被混为一谈；水位可能错误推进，后续也没有可解释补偿。 |
| P1-8 | 多处全量替换或读后写不是原子操作。 | app/fund/crud/fund_crud.py:19-24；app/trade_calendar/crud/calendar_crud.py:14-20；app/fund_manager/fetch/worker.py:123-124；app/fund_detail/crud/detail_crud.py:69-76；app/fund_nav/crud/repair_crud.py:26-67。 | 进程中断、并发触发或唯一键竞争时可能得到半套目录、空日历、经理历史被删后未恢复、重复 repair 记录。 |
| P1-9 | MySQL 通用 batch_insert 对重复键更新全部字段，未显式区分“本次字段缺失”和“应清空字段”。 | app/db/mysql.py:729-763，尤其 740-745。 | 部分源返回的 NULL/缺失字段可能覆盖历史非空事实；不同域未统一定义 patch/replace 语义。 |
| P1-10 | 修复队列没有真正的死信和租约模型；事件失败还写入 task_kind='adj'。 | app/fund_nav/crud/repair_crud.py:118-179；scripts/nav_repair_pass.py:55-79；app/fund_nav/fetch/events_worker.py:124-134。 | 失败会反复进入可重试集合，人工无法按域、源、错误类别接管；事件和复权的 SLA、重试策略相互污染。 |
| P1-11 | 治理 shell 脚本吞掉阶段返回码。run_stage 无论子命令结果如何都返回 0，后续阶段继续执行。 | scripts/run_nav_gov_full.sh:39-46；scripts/run_nav_events_adj.sh:40-45。 | 日志看似 done 但实际某一阶段失败；调度器无法告警，且失败阶段之后可能继续对全库写入。 |
| P1-12 | 多个批处理脚本硬编码 SQLite，且以编号区分批次；与已迁移 MySQL 的正式入口并存。 | scripts/holdings_batch.sh:11-24；scripts/holdings_batch2.sh:9-16；scripts/holdings_batch3.sh:9-32；scripts/run_nav_events_adj.sh:54-61。 | 可能读旧库统计、把“覆盖率”误判为生产结果；脚本行为不可组合、不可审计，批次编号无法表达业务语义。 |
| P1-13 | 季度持仓调度用月份/日期硬编码窗口，不以披露批次或数据源可用状态决定；每日 08:00 大部分日期只是正常退出。 | scripts/quarterly_holdings_sync.sh:28-50。 | 监管披露节奏变化、节假日或延迟披露时会漏拉；调度成功码不代表持仓新鲜。 |
| P1-14 | data_selfcheck.py 质量规则不可持续消费：净值窗口硬编码 2026-07-15；检查了若干不存在/非本域表并吞异常；--json 输出后仍打印文本，且发现问题不返回非零码。 | scripts/data_selfcheck.py:105-129、153-186。 | 无法稳定接入 dashboard、告警和 CI/调度；检查口径会随日期失效，表不存在与真实异常无法区分。 |
| P1-15 | Tushare 探针实际请求两次，且治理脚本依赖轮询探测后再跑长时间全量任务。 | scripts/probe_tushare.py:27-29；scripts/run_nav_events_adj.sh:19-38。 | 探针本身消耗配额；“连续成功”后全量事件/复权仍可能因长窗口失效，缺少一次 run 的预算、截止时间和取消机制。 |

### P2：维护成本、性能和长期演进问题

| 编号 | 问题 | 文件:行号证据 | 影响 / 判断 |
|---|---|---|---|
| P2-1 | daily_nav_sync.py、nav_repair_pass.py、backfill_etf_linkage.py、run_nav_events_adj.sh、run_nav_gov_full.sh、holdings_batch1-4.sh 命名混合了动作、领域、历史批次和运行方式。 | scripts/ 文件清单；典型证据：scripts/daily_nav_sync.py:1-23、scripts/run_nav_gov_full.sh:1-3。 | 无法从文件名判断职责、输入、是否写库、是否可自动调度；重复脚本持续分叉。 |
| P2-2 | fund_nav 每次写入前读取该基金全部历史再逐行合并；事件复权计算也按历史净值与事件朴素组合。 | app/fund_nav/crud/nav_crud.py:43-88；app/fund_nav/fetch/adj_engine.py:64-117。 | 历史越长，单基金成本越高；全库回补和每日写入都会产生不必要的数据库读放大。 |
| P2-3 | 通用 worker 一次性向线程/进程池提交全部目标，incremental 还把“上一季度持仓”硬编码进通用基类。 | app/common/worker_base.py:123-154、197-230。 | 大任务的内存、future 数量、取消与断点语义不可控；通用层携带领域规则，后续难以拆分。 |
| P2-4 | 回补/事件脚本运行时调用 init_db 或动态建表。 | scripts/backfill_etf_linkage.py:59-61；app/fund_nav/fetch/events_worker.py:39-47、96-105。 | schema 变更与业务任务耦合；权限、锁、版本和回滚责任不清。 |
| P2-5 | 运行日志以普通文本追加，净值 cron 日志已超过百 MB，详情任务还使用回车覆盖式进度输出。 | scripts/daily_nav_sync.py:185-199；logs/daily_nav_cron.log；logs/detail_cron.log。 | 不能按 run/source/code 可靠检索、聚合和告警；无统一 rotation/retention，事故时日志本身成为资源风险。 |
| P2-6 | 详情源在代码注释、数据清单和治理脚本中命名不一致。 | app/fund_detail/fetch/worker.py:1-2；scripts/run_nav_gov_full.sh:61-62；docs/data-structure-inventory.md:61-74。 | 不能确定授权、限速和故障联系人；重构前必须以接口契约和样本确认真实源。 |

## 5. 重构目标与第一性原理

### 5.1 问题定义

要解决的是：在外部源不稳定、基金披露节奏不同、数据库连接有限的条件下，持续把“可验证的基金事实”写入生产 MySQL，并且每次执行都可暂停、重试、对账和解释。

### 5.2 需要推翻的默认假设

- “最新净值日期等于该基金已经完整同步”：不成立，可能是源延迟、空响应、解析失败或基金自身披露滞后。
- “upsert 就等于幂等”：不完全成立；业务键只保证重复写不新增，还需要字段 patch 语义、提交水位和错误状态。
- “thread-local 就是连接池”：不成立；当前实现是每线程持有连接的注册表，不具备上限和归还。
- “所有基金每天同一频率、同一源策略”：不成立；QDII、FOF、货币、海外资产和新成立基金有不同披露日历。
- “重试所有异常最稳”：不成立；认证、参数、解析和结构变化应快速失败，限流/超时才适合退避。
- “复权是净值成功后的顺手维护”：不成立；复权有独立数据源、成本、SLA 和受益对象，应按影响范围维护。

### 5.3 不可再违反的基本事实

1. 所有外部调用都可能超时、返回空、返回半结构化数据或被限流。
2. DB 连接、socket、线程和上游配额都是有限资源，必须有显式预算。
3. 只有“事实写入与对应水位同一事务提交”后，才能推进水位。
4. 每一次尝试必须留下持久化状态，重启后靠状态恢复而不是猜测。
5. dashboard 需要结构化的 run/item/quality 指标，普通日志只能作为诊断附件。

## 6. 目标架构

### 6.1 分层职责

```text
Scheduler / CLI / API
        │  job_name + logical_date + window + shard
        ▼
Run lease / target snapshot
        │  有界分页、租约、截止时间
        ▼
Target planner + fund_sync_state
        │  source policy / watermark / publication calendar
        ▼
Source adapters
        │  HTTP client、限速、重试分类、熔断、原始响应摘要
        ▼
Normalizer + validator
        │  typed row、业务键、空响应/异常值/未来日期检查
        ▼
Domain sink / repository
        │  staging 或批量 upsert；事实与水位同事务
        ├──────────────► sync_item / metrics
        └──────────────► retry queue / dead-letter
                         │
                         ▼
                  quality checks + dashboard + alerts
```

建议新增一个共享 app/sync_engine/（也可以暂命名 app/fund_sync/）承载横向能力，现有 app/fund_nav、app/fund_holdings 等保留领域适配器和 repository，不把所有领域规则塞进 worker_base。

1. **调度层**：只负责触发命名 job、设置逻辑日期/窗口、超时和退出码；不在脚本里无限轮询。
2. **租约与 run 层**：创建 run_id，按 (job_name, logical_date, window, shard) 防重；记录 owner、heartbeat、lease expiry、deadline。
3. **目标规划层**：按基金类型、披露日历、源可用性和水位生成可重复的目标快照；采用 keyset 分页，不一次性提交全库 future。
4. **源适配层**：每个源只实现请求、响应解析和源错误映射；不得在 adapter 内直接写业务表。
5. **标准化/校验层**：统一日期、数值、代码、单位、缺失字段、源版本和质量结果。
6. **落库层**：领域 repository 负责事务、业务键、字段 patch/replace 语义；数据和水位一起提交。
7. **修复层**：统一错误分类、指数退避、最大尝试次数、租约和死信；按领域拆分 nav/detail/holdings/events/adj。
8. **质量层**：将 run/item 统计、数据质量指标和资源指标写入可查询的结构化载体，日志只是补充。

### 6.2 推荐状态表

不建议把“每只基金的最新日期”直接加到 fund_nav 事实表。建议新增或规划以下最小模型：

```text
sync_run
  id, job_name, logical_date, window, shard, status,
  started_at, heartbeat_at, finished_at, deadline_at,
  target_count, success_data_count, success_empty_count,
  skipped_count, failed_count, dead_count, rows_written,
  source_calls, retry_count, error_summary_json

fund_sync_state
  domain, entity_code, source, watermark_date, coverage_end,
  last_status, last_reason, last_run_id, attempts,
  next_retry_at, last_success_at, updated_at
  UNIQUE(domain, entity_code, source)

sync_item（可按量保留明细，或只保留失败/抽样）
  run_id, domain, entity_code, source, target_date,
  watermark_before, watermark_after, status, error_class,
  http_status, rows_fetched, rows_written, latency_ms,
  attempts, message, created_at, finished_at

sync_repair_queue
  domain, entity_code, task_kind, source, gap_start, gap_end,
  status(pending/running/succeeded/dead), attempts,
  next_retry_at, lease_owner, lease_until, error_class,
  last_error, first_failed_at, last_attempt_at
```

过渡期可以扩展 nav_repair_queue，但必须把 domain/source/error_class/lease/dead 补齐；事件不得继续伪装成 adj。MySQL 下用事务 + SELECT ... FOR UPDATE SKIP LOCKED（确认版本支持后）领取队列，或采用单源单 worker 避免重复领取。

### 6.3 运行资源预算

以可验证的预算替代“并发度越高越快”：

- Web 进程连接池、同步进程连接池、临时维护进程池分别设上限，合计不得超过 MySQL max_connections 的安全余量；例如先按 Web 4 + 每个同步 job 2-4 连接做容量评估，而不是把 8 个线程映射成 8 个永久连接。
- 每个 job 一个有界执行器；默认 4-8 个同步 worker，按源单独 semaphore；不按 50 只基金反复创建线程池。
- 每批目标通过队列/窗口提交，例如 100-500 个 item，完成一批就释放对象并写 checkpoint；不能一次性提交 27408 个 future。
- 将 DB pool checked_out、pool wait、HTTP keepalive 数、进程 FD 数和线程数作为运行指标；达到预算时降速或暂停，而不是继续重试。

## 7. 连接与 HTTP 管理方案

### 7.1 MySQL

将 app/db/mysql.py 的 thread-local 模式改成明确的 bounded pool，优先使用 SQLAlchemy QueuePool 或经过验证的 PyMySQL pool；若保留自研抽象，至少提供：

```python
with database.connection() as conn:
    with conn.transaction():
        # execute / executemany
```

约束：

- checkout 有超时，连接回收必在 finally；连接 ping 失败时只替换这一连接。
- 连接设置最大寿命/空闲寿命，应用退出、SIGTERM、worker 进程初始化时显式 close；子进程绝不继承父进程已有连接。
- “连接池”不等于“连接列表”：禁止把每次创建的连接永久追加到 _connections。
- domain repository 不再直接拿隐式线程连接；事务对象绑定一个明确连接，数据写入和 fund_sync_state 推进使用同一连接。
- 对 batch upsert 采用参数化 SQL 和列级 patch 语义：缺失字段不覆盖旧值；只有明确的业务清空才写 NULL。
- 启动时 schema 迁移由独立 migration/发布步骤负责，不能由回补脚本或首次调用的 worker 动态建表。

### 7.2 HTTP

- 每个源一个进程级客户端（同步阶段可以是 requests.Session，异步阶段再换 httpx.AsyncClient），配置连接上限、keepalive、连接/读取超时和关闭钩子。
- 统一封装 headers、User-Agent、Referer、代理（如需要）、响应大小上限和 Retry-After；禁止各 adapter 自己 requests.get 后各自解释异常。
- AkShare 是同步 SDK：初期将 AkShare 调用限制在一个小型、可回收的 adapter worker 中；避免通过模块级 monkey patch 产生多个未关闭 Session。若必须 patch，集中在进程启动/退出钩子并可观测其生命周期。
- 原始响应不必全量落库，但至少记录 endpoint、HTTP status、响应 hash/size、解析版本、耗时和错误摘要，便于离线重放。

## 8. 增量、幂等与断点续跑

### 8.1 每日净值主链路

1. 由交易日历和源发布策略确定 base_trade_date；不同资产类别允许不同的 publication_lag，不要假定所有基金在同一时刻发布。
2. 规划器按 SQL/keyset 分页找目标：有 fund_sync_state 时按水位找；过渡期用 funds 左连接按代码聚合 MAX(fund_nav.trade_date)，并把结果写入状态表，避免每个 worker 再查一次。
3. 每只基金只请求主源东财 F10 增量；成功解析并通过校验后，在一个事务中 upsert fund_nav/fund_cum_return、写 item、推进水位。
4. fund_nav 的 (fund_code, trade_date) 唯一键继续保留；更新时对 nav/acc_nav/daily_return/cum_return 使用完整行或列级 patch，不能让半结构化响应的缺失字段覆盖旧值。
5. rows=[] 必须结合源响应和基金发布策略，写成 success_no_data 或 source_not_ready，记录下一次可重试时间；不得无条件推进到 base date。
6. 发现水位与日历之间有缺口时创建 nav repair item；水位只向前推进，不因一次旧数据回补覆盖更近的水位。
7. AkShare/JS 仅作为隔离的历史回补或明确的单基金人工降级。若必须自动 fallback，只允许一次、有独立预算，并将实际源写入 item；不得把三层 fallback 作为每只基金的默认路径。
8. 一次 run 被 SIGTERM/进程崩溃后，已提交的基金靠 state 识别为完成，未提交的 item 仍为 pending/running-expired；重启继续剩余目标，不靠全量夜间轮询。

建议的目标查询形态（字段名以最终 schema 为准）：

```sql
SELECT f.code
FROM funds AS f
LEFT JOIN fund_sync_state AS s
  ON s.domain = 'fund_nav'
 AND s.entity_code = f.code
 AND s.source = :source
WHERE f.code IS NOT NULL
  AND (s.watermark_date IS NULL OR s.watermark_date < :base_trade_date)
ORDER BY f.code
LIMIT :page_size;
```

### 8.2 其他数据域策略

| 数据域 | 新的目标判据 | 推荐提交语义 | fallback / 频率 |
|---|---|---|---|
| 基金目录 | 目录快照版本/抓取日期变化 | 新快照 staging 校验通过后事务切换；不能先删正式表 | 每日或每周低频；空快照直接失败并告警 |
| 交易日历 | 当前日期距最新已知交易日的 freshness | staging 后校验连续性，再原子替换/合并 | 每日早间；源失败保留旧日历，但标记 stale，不能让调用方静默使用未知日历 |
| 基金详情 | last_success_at + 7d、源更新时间或字段缺失 | 逐字段 patch；源不可用写状态表，保留上一份有效详情 | 每日 20:10 可保留，但按分页/限速；连续失败进 detail repair |
| 季度持仓 | 目标披露季度 + fund/quarter/type 覆盖状态 | 以 (fund, quarter, holding_type) 为替换单元，在单事务中 delete+insert；空持仓需有 coverage marker | 按披露日历和源可用性触发，只抓缺失季度，不默认回抓两年 |
| 经理任职史 | source hash/更新时间或周 freshness | 先解析到 staging，单基金事务内替换；失败保留旧值和失败状态 | 每周/按需；显式重试，不做无边界每日全量 |
| 分红/拆分事件 | 上次事件水位、公告日期、ex-date 窗口 | 独立 fund_events item；事件写入与事件水位同事务 | 周期性低频；事件失败只进 events 队列 |
| 复权净值 | 事件变更或重点基金集合变化 | 只重算受影响基金/日期，记录 adj_src 和计算版本 | 与 nav 解耦；Tushare 单独配额和 repair 队列 |
| ETF 关联/行业 | 基金目录、详情、持仓变更触发；定期全量校准 | 全量计算先 staging，校验后原子替换；行业批量 upsert | 不进入每日净值链路；按依赖变化或周/月校准 |

## 9. 统一反爬、重试与降级策略

新增 SourcePolicy 概念，每个 source + endpoint 至少配置：并发上限、最小间隔/QPS、连接/读取超时、最大尝试次数、可重试 HTTP 状态、异常类别、熔断窗口、单 run 配额和降级目标。

### 9.1 错误分类

| 类别 | 例子 | 处理 |
|---|---|---|
| rate_limited | 429、明确限流文本、配额耗尽 | 尊重 Retry-After，指数退避 + jitter；超过预算进入 delayed/dead，不切换到高并发 fallback |
| transient_network | connect/read timeout、连接重置、5xx | 有上限的 2-4 次重试；记录每次延迟和最终源 |
| auth_or_quota | token 无效、账户配额不足、权限错误 | 立即熔断该源，告警，不对每只基金重复重试 |
| malformed | JSON/schema/日期/数值解析异常 | 保存样本摘要，停止该 adapter 的自动级联；修复解析器或人工回补 |
| source_not_ready | 页面可达但本基金今日尚未发布 | 不推进水位，按基金/源发布策略延后一次，不能立即多轮重打 |
| permanent_unavailable | 明确无该基金/接口不支持 | 标记源不可用或不适用，设置较长复核周期，不进高频 retry |
| db_transient | deadlock、连接断开 | 回滚当前事务后有限重试；若超预算，item 留 pending，不能已写数据但未写水位 |

### 9.2 各源建议基线

- **东财**：先以单一 F10 endpoint 做 canary，初始采用全局（跨 job）低并发和约 0.3-0.5 秒最小间隔，再根据实测响应、429/5xx 和完成窗口校准；这只是起始值，不是上游 SLA。日净值和历史回补共享同一 source budget。
- **AkShare/详情/持仓**：并发先控制在 2-4；按实际底层 endpoint 单独配策略。不能因一个 AkShare 接口空结果就同时启动多个同源接口；字段级部分成功要带 provenance。
- **Tushare**：一个专用队列和账户配额，不允许 events、adj、probe 各自并发。当前按约 1 req/s 的礼貌基线规划，实际以账户限制和 canary 校准；token/接口错误立即熔断。探针只执行一次请求。
- **跨进程**：优先让同一 source 只有一个受控 worker；若必须多进程，使用 Redis/DB 租约实现全局 token bucket，而不是每个进程自带 limiter。
- **降级**：降级必须是“主源明确失败 + 单次预算 + 可观测结果”，不能把 fallback 当成功遮罩。源返回空、解析失败和认证失败不能共用同一 fallback 条件。

## 10. 调度与执行模型

### 10.1 推荐调度表

系统已有 systemd 服务但没有同步 timer；建议选择 systemd timer 作为同步唯一调度源，若暂时保留 cron，也必须由同一 manifest 生成并使用 flock/超时包装，不能两套并存。

| Job | 建议时间/频率 | 执行内容 | 退出/重叠规则 |
|---|---|---|---|
| sync_fund_nav | 工作日 20:00；Tue-Sat 08:00、12:00 | 每次一次目标规划 + 有界分页 + 一次小 repair slice | 同一 logical date/window 只能一个 run；有 deadline；不在进程内 5 分钟全量轮询 |
| repair_fund_nav | 每 30-60 分钟，限定窗口；或每个 nav run 结束后一次 | 领取 pending/delayed nav item，按源预算重试 | lease + max attempts；dead 后告警，不无限重试 |
| sync_fund_holdings | 每日 08:00 触发 planner | 依据披露日历只抓目标季度 | 无披露目标时返回 success_no_target，不是空跑成功；窗口来自配置/日历 |
| sync_fund_detail | 每日 20:10 或 freshness 驱动 | 分页刷新过期/字段缺失详情 | 与 nav 独立 source budget；失败可单独 repair |
| sync_fund_manager | 每周或变更驱动 | 低频刷新经理任职史 | 单基金原子替换；失败保留旧快照 |
| sync_fund_events | 每日低频/每周 | 增量事件扫描 | 与 adj 分离；Tushare 专用队列 |
| repair_fund_adjusted_nav | 每日低频/按影响集合 | 重点基金或受事件影响区间复权 | 不由每日 nav worker 同步调用 |
| sync_trade_calendar | 每工作日早间/每周校准 | 更新交易日历 | 源失败保留旧表但产生 stale 告警 |
| selfcheck_fund_data | 每小时轻量 + 每日全量质量 | MySQL 生产库质量指标 | 根据阈值非零退出；只读、限时、可采样 |
| backfill_* | 手工且显式参数 | 指定域、代码、日期或 shard | 默认 dry-run/需要确认；独立限速，不与日常 job 争资源 |

### 10.2 调度实现要求

- 每个 job 只定义一个 Python entrypoint；shell 只能负责环境、日志、超时和退出码。
- 使用 DB lease 或系统级锁防止“cron + API + 手工”同时运行；任务表中的 running 不能只靠查询判断，要有唯一约束/租约过期。
- 设 TimeoutStopSec/timeout，收到 SIGTERM 后停止领取新 item，等待当前事务结束并将租约标记为可恢复。
- shell 的 run_stage 必须返回子进程真实 rc；任一关键阶段失败应使 job 失败或明确标记 partial，不能继续执行全库后续阶段并返回 0。
- 日历、净值发布窗口、季度披露窗口和时区均配置化；不要把月份 1/4/7/10、20-31 这类经验规则写死在 shell。

## 11. 监控、自检与 dashboard 数据契约

### 11.1 每次 run 必须输出的指标

sync_run 至少提供：run_id/job_name/logical_date/window/status/start/end/duration/target_count，以及 success_with_data/success_no_data/skipped/fail/dead、写入行数、源调用数、重试数、限流数、HTTP 状态分布、DB 等待/提交耗时、最大并发、连接池高水位、进程 FD 高水位。

sync_item/状态表至少提供：基金代码、领域、源、目标日期、水位前后、状态、错误类别、尝试次数、下次重试、最后错误、请求耗时、抓取行数、落库行数。错误消息要脱敏，不能写 token/cookie。

### 11.2 数据质量指标

- 净值：各基金类型/源的最新日期 lag、目标日覆盖率、缺失交易日、重复业务键、nav<=0、累计净值关系异常、fund_cum_return 配对率。
- 复权/事件：adj_nav 覆盖率、adj_src 分布、受事件影响基金完成率、事件队列年龄和 dead 数。
- 详情/经理/持仓：freshness 分布、源不可用数量、目标季度覆盖率、空响应率、持仓比例越界、经理快照原子替换失败数。
- 日历/目录：最新日期、历史连续性、目录快照行数变化、空快照保护次数。
- 运行资源：DB pool exhausted/wait、HTTP 连接数、FD、线程、任务超时和重叠拦截数。

### 11.3 data_selfcheck.py 重构要求

保留脚本名作为兼容入口，但内部改为调用统一 app.db，从配置连接当前目标库（MySQL 或明确指定的测试库），不默认读取旧 SQLite。具体要求：

1. --json 时 stdout 只输出一个合法 JSON object；日志写 stderr 或结构化日志文件。
2. 日期窗口由“最新交易日/当前时间”计算，支持 --as-of 和测试注入，不写死日期。
3. 表名/字段来自 schema contract；不存在的表标记 not_applicable，不能吞成 NULL。
4. 提供 --fail-on=critical|warning|never，关键指标超过阈值返回非零，便于 systemd/cron 告警。
5. 可将每次质量结果写入 data_quality_metric（或 sync_quality_metrics），dashboard 只读该契约，不解析日志。
6. 近期窗口、抽样和全量扫描分层，所有查询有超时/上限；不为自检再次打开不受控的 SQLite 快照连接。

建议初始告警：P0 Errno24 > 0 立即告警；nav run failure ratio > 5% 或连续两轮失败告警；目标日覆盖率低于基线、repair oldest age 超阈值、dead>0、calendar stale、pool exhausted、FD 高水位超过预算均告警。阈值要在一周基线后配置化。

## 12. 脚本命名与目录重构

### 12.1 命名规范

Python 文件统一采用“动作_领域_实体”，动作只允许少数枚举：

sync_（正常同步）、backfill_（历史回补）、repair_（失败修复）、selfcheck_（质量检查）、probe_（源探测）、mark_（人工/运维标记）。

不使用 batch1/2/3/4、run_nav_gov_full 这种无法表达边界的名字；窗口、源、代码集合通过参数或 job 配置表达。Shell 不承载业务编排，若必须存在，只使用 run_<canonical_job>.sh 作为极薄包装。

### 12.2 推荐目录

```text
scripts/
  sync/
    sync_fund_nav.py
    sync_fund_detail.py
    sync_fund_holdings.py
    sync_fund_manager.py
    sync_trade_calendar.py
    sync_stock_industry.py
    sync_fund_events.py
  repair/
    repair_fund_nav.py
    repair_fund_detail.py
    repair_fund_holdings.py
    repair_fund_events.py
    repair_fund_adjusted_nav.py
  backfill/
    backfill_fund_nav.py
    backfill_fund_events.py
    backfill_fund_etf_linkage.py
  maintenance/
    selfcheck_fund_data.py
    probe_source_tushare.py
    mark_fund_detail_source_unavailable.py
  runners/                 # 仅环境、锁、超时、日志；不放业务逻辑

app/sync_engine/
  orchestration/           # runner, planner, lease, cancellation
  state/                    # run/item/watermark/repair repositories
  sources/                  # eastmoney, akshare, tushare, detail source
  normalize/                # typed models and validators
  sinks/                    # domain repositories and staging
  policies/                 # rate limit, retry, circuit breaker
  observability/            # structured log, metrics, quality

app/fund_nav/               # nav-specific parser/repository/domain rules
app/fund_holdings/          # holdings-specific parser/repository/domain rules
...
deploy/systemd/             # service/timer unit and one job manifest
```

### 12.3 旧入口迁移映射

| 旧入口 | 目标入口 | 迁移策略 |
|---|---|---|
| daily_nav_sync.py | sync/sync_fund_nav.py | 旧文件保留一个版本，转发参数并输出 deprecation；切换后删除内部夜间轮询 |
| nav_repair_pass.py | repair/repair_fund_nav.py | 改为通用 lease/queue consumer；旧命令只做兼容转发 |
| backfill_worker.py / nav backfill | backfill/backfill_fund_nav.py | CLI 保持 ifund sync/backfill fund-nav 统一入口，按代码/日期/shard 参数 |
| run_nav_events_adj.sh | sync_fund_events + repair_fund_adjusted_nav | 拆成两个 job；禁止一个脚本串联事件、复权和计算 |
| run_nav_gov_full.sh | 多个独立 job | 删除“nav governance”宽泛语义；每个阶段分别有 run/status/告警 |
| quarterly_holdings_sync.sh | sync/sync_fund_holdings.py | 披露日历配置化；shell 只保留 systemd/cron 兼容包装 |
| holdings_batch*.sh | 一个 backfill_fund_holdings.py | 类型、shard、日期作为参数；删除编号脚本和 SQLite 统计 |
| data_selfcheck.py | maintenance/selfcheck_fund_data.py | 保留兼容壳，改为 MySQL/配置目标和纯 JSON 契约 |
| probe_tushare.py | maintenance/probe_source_tushare.py | 单次调用、统一 source policy，不再被长轮询脚本无限等待 |

## 13. 分阶段实施步骤

### 阶段 0：止血与基线（实施前置）

- 盘点所有主机上的 cron、systemd、bgjob 和人工入口，建立 job manifest；确认 MySQL max_connections、应用进程 FD 上限和当前连接数。
- 在连接层改造完成前，不运行大规模 nav/holdings backfill；未来若需止血回补，必须使用有界单进程、指定代码/分片和独立测试/生产审批。
- 固化事故回归基线：Too many open files、目标数、失败数、FD 高水位、DB 连接高水位、每源请求数。
- 明确真实详情源、东财接口字段契约、Tushare 配额、基金类型发布滞后策略和复权需求集合。

**验收**：有一份可审计的调度清单；所有同步入口都能标注 owner、数据源、落库表、是否写库和资源预算。

### 阶段 1：同步基础设施

- 实现 bounded MySQL pool、HTTP client 生命周期、统一 SourcePolicy、错误分类、结构化日志。
- 建 sync_run、fund_sync_state、sync_item/repair 队列和迁移脚本；统一状态枚举。
- 将 data_selfcheck 改为目标库无关但配置驱动，增加纯 JSON、退出码和质量指标。
- 先用 fake source 和测试 MySQL 验证连接归还、事务回滚、SIGTERM 恢复，不接真实抓取。

**验收**：固定规模压力测试中 FD、线程和 pool checked-out 在预算内稳定；进程退出后连接全部关闭；失败不会留下半提交水位。

### 阶段 2：净值 canary

- 实现 sync_fund_nav：SQL 目标规划、东财 F10 主源、标准化、幂等写入、state 推进、nav repair。
- 暂不在 nav worker 内触发 events/adj；AkShare/JS 只做明确的隔离回补。
- 先跑 10/100/1000 只合成或指定基金，对比旧库和源样本；随后在生产采用小 shard canary，观测至少 3-5 个交易窗口。
- 新旧链路只允许“读/对账并行”，不要同时向同一事实表写相互覆盖的结果。

**验收**：重跑不增加重复行；中断后能从 item/state 续跑；空结果不推进错误水位；源错误分类和 repair 统计准确；FD 不随目标数增长。

### 阶段 3：迁移其他数据域

按依赖顺序迁移：交易日历/基金目录 → 详情 → 持仓 → 经理 → 事件 → 复权 → ETF 关联/行业。每个域完成独立 adapter、repository、state、repair 和 quality 指标后再接调度。

- 目录和日历先解决 staging/原子替换。
- 详情、经理解决保留旧快照、字段 patch、来源不可用状态。
- 持仓解决季度目标和空覆盖 marker，移除两年全量默认策略。
- events/adj 彻底拆 job，只对受影响集合/重点集合运行。
- ETF/行业从每日 nav 中剥离，改为依赖变更触发或低频校准。

### 阶段 4：调度与 dashboard 切换

- 部署唯一的 systemd timer 或生成式 cron；加入 lease、超时、重叠拦截、真实退出码和日志 rotation。
- dashboard 改读 sync_run、state、repair、quality metric；告警以阈值和趋势为准。
- 新旧结果做只读对账至少 3-5 个交易日；重点检查 2026-09-01 事故类型、QDII/FOF 延迟、基金新增、节假日和源返回空。
- 分域切换正式写入，先关闭旧脚本对应调度，再删除旧路径的自动触发，避免双写。

### 阶段 5：清理与收敛

- 保留旧文件兼容壳一个版本周期；在文档、crontab、systemd、README、运维 runbook 中全部替换 canonical 名称。
- 删除编号批处理、宽泛治理脚本和运行时建表逻辑；把 schema migration、source policy、job manifest 纳入发布物。
- 复盘连接、FD、上游配额和数据质量基线，调整并发/限速；形成新域接入模板。

## 14. 测试计划

| 测试层 | 重点用例 | 通过标准 |
|---|---|---|
| 单元测试：连接/HTTP | checkout/归还、ping 失败替换、事务异常回滚、shutdown close、子进程不继承连接、Session close、pool 超时 | 无泄漏；异常后连接可复用；pool/FD 有上限 |
| 单元测试：规划/水位 | 无历史、新基金、已最新、源滞后、日历假日、重复 run、分页/重启、watermark 单调性 | 目标集合可重复；只在事实提交成功后推进水位 |
| 单元测试：adapter | 正常、空响应、字段缺失、格式变化、429、5xx、超时、认证错误、未来日期、负净值 | 错误分类正确；不把空响应/解析错误伪装成成功 |
| 单元测试：sink | 同一业务键重复写、NULL patch、部分字段、批量失败、delete+insert 原子性、deadlock 重试 | 幂等；旧非空字段不被缺失字段覆盖；失败全回滚 |
| 队列/并发集成 | 多 worker 领取同一 item、lease 过期、SIGTERM、最大 attempts、dead-letter、跨 job source budget | 不重复执行；可恢复；dead 可查询和告警 |
| MySQL 集成 | 使用隔离测试库测试唯一键、事务、SKIP LOCKED/替代方案、连接池上限和索引计划 | 不连接生产；并发结果可对账；查询不退化为全历史扫描 |
| 源契约/回放 | 保存脱敏 fixture 或 VCR/replay，覆盖东财、AkShare、Tushare、详情/持仓 HTML/JSON | CI 不访问真实上游；解析版本变更可检测 |
| 资源压力 | 用 fake source 模拟 27k 目标、随机失败/延迟/空响应，观察 /proc/<pid>/fd、线程、DB/HTTP 连接 | 目标数增加不导致资源线性泄漏；能在 deadline 前受控结束 |
| 数据对账 | 新旧链路抽样比较净值、累计收益、季度持仓、详情 freshness、adj 样本、事件影响范围 | 重复数为 0；缺口可解释；差异有 source/版本/时间原因 |
| 调度验收 | 重叠触发、超时、节假日、时区、跨午夜、失败码、服务重启、timer catch-up | 只有一个有效 run；失败可告警；不会吞 rc 或无限等待 |
| 质量/可观测 | JSON 单对象解析、dashboard 查询、阈值退出码、日志 rotation、错误脱敏 | 机器可消费；严重质量问题返回非零并产生告警 |

CI/测试环境不得调用真实抓取，也不得连接生产 MySQL。生产 canary 只在实施阶段、审批后以指定代码/小 shard 执行；本次评审没有执行这些动作。

## 15. 实施优先级与最终验收清单

优先级是：

1. 连接池、HTTP 生命周期、执行器上限、FD/DB 资源指标；
2. run/lease/state/repair 数据模型和纯生产库自检；
3. NAV 单源增量 canary，移除每日链路中的 adj/events；
4. 其他领域的原子写入和独立调度；
5. 统一命名、删除编号脚本、dashboard 和旧入口下线。

整体重构完成的判据：

- 任何 job 都能回答“何时、哪个窗口、哪些目标、哪个源、写了多少、失败为何、何时重试”；
- 目标规模从 100 增长到 27408 时，FD、线程、DB 连接和 HTTP 连接保持在配置预算内；
- 进程中断/重启不会丢失已提交状态，也不会重复推进水位；
- 每个领域的空响应、源不可用和真正失败均可区分；
- nav、events、adj、detail、holdings、manager 的调度、队列和告警互不伪装、互不吞错；
- dashboard 使用结构化质量指标，不再依赖旧 SQLite 快照或日志文本猜测生产健康度。
