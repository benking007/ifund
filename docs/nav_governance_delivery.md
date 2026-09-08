# iFund 净值数据治理 M1–M6 交付说明

交付日期：2026-08-30

本次在 `/root/workspace/ifund` 完成净值数据治理的 schema、回补 worker、前复权引擎、事件采集、增量维护、失败重试、统计和回归测试。全量回补没有在本次验证中启动；仅执行了单基金小样本。

## 结论

- M1–M6 代码已落地，启动迁移已在实际 `backend/data.db` 上执行完成。
- `fund_nav` 已增加 `adj_nav`、`adj_src`；`fund_div_split`、`nav_repair_queue` 及索引已创建。
- `/api/stats` 新增 `adj_nav_coverage`、`zero_nav_funds`、`repair_pending`、`repair_failed`。使用新代码的 Flask test client 实测返回 HTTP 200；统计查询通过 immutable SQLite 连接和覆盖/联合索引执行。
- `api/fund/{code}/nav` 保留原 `date`/`nav` 字段，新增 `adj_nav`/`adj_src`。
- 当前已运行的 8003 waitress 进程是在本次代码修改前启动的旧进程，因此要让线上 8003 立即看到新 `/api/stats` 字段，需要按现有发布流程重启/滚动部署；没有在本次任务中强制重启线上进程。

## M1：schema 与 CRUD

`schema_sqlite.sql` 中：

- `fund_nav`：`adj_nav FLOAT`、`adj_src TEXT`，并增加复权统计/写回索引。
- `fund_div_split`：按 `(fund_code, ex_date, event_type)` 唯一，记录分红现金、拆分比例、来源和抓取时间。
- `nav_repair_queue`：支持 `nav`/`adj`、`pending`/`running`/`done`/`failed`，按 `(status, next_retry_at)` 建索引。

`app/main.py` 启动时先幂等尝试迁移 `fund_nav` 两列，再执行幂等 schema，最后复用同一迁移函数处理 `portfolios.cap`。实际大库迁移完成，未改变既有净值行。

新增/扩展 CRUD：

- `backend/app/fund_nav/crud/nav_crud.py`
- `backend/app/fund_nav/crud/repair_crud.py`
- `backend/app/fund_nav/crud/div_split_crud.py`

净值写入按基金事务处理；NAV 增量写入会保留既有 `adj_nav`/`adj_src`，Tushare 写入只在单位净值缺失时补 `unit_nav`/`accum_nav`。

## M2：NAV 全史回补

实现文件：`backend/app/fund_nav/fetch/backfill_worker.py`。

- 复用 `worker_base.main`，东财 `fetch_nav_full` 为主通道。
- 基金间请求槽位至少间隔 300ms；单基金初次请求加 3 次重试，指数退避后写失败队列。
- CLI 未给 `--all` 且没有显式代码/类型时，只消费 `nav_repair_queue` 的 pending NAV 任务。
- 20:00–23:00 超过 50 只的批量会被拒绝；日常同步的大类型也拆成 50 只以内的批次。

## M3：adj_nav 与事件复权

实现文件：`backend/app/fund_nav/fetch/adj_engine.py`、`tushare_client.py`。

- Tushare `fund_nav` 通过 JSON-RPC POST 读取 `unit_nav`、`accum_nav`、`adj_nav`，代码自动转换为 `CODE.OF`。
- token 只从运行环境或 `/root/workspace/ai-agent-platform/services/fin_data/.env` 读取，不硬编码、不打印。
- 自算采用已确认的最新日基准公式：事件除权日前的历史净值使用之后事件因子，结果为 `nav(t) * f(t) / f(latest)`，写入 `adj_src='calc'`。
- `cross_check` 对 Tushare 行均匀抽样，分别记录 `comparable_samples`、
  `incomparable_samples` 和 `mismatch_samples`；相对误差超过 0.5% 的口径不一致样本逐条记录
  WARNING，并在 `mismatch_count`/`warning_count` 中统计；无可比样本也会告警，不会被 CLI 计为成功。
- Tushare 请求异常或返回空/无有效复权行时，按基金写入 `nav_repair_queue(task_kind='adj')`；队列写入失败也只记 WARNING，
  不隐藏原始失败。

## M4：分红/拆分事件

实现文件：`backend/app/fund_nav/fetch/events_worker.py`。

通过 Tushare `fund_div`/`fund_split` 转换为本地统一事件行，按唯一键幂等 upsert。东财 `fhsp` 页面可访问，但本次未逆出稳定的 JSON 数据端点，因此未将 HTML 页面解析作为主数据源。

## M5：增量与 repair pass

- `scripts/daily_nav_sync.py`：每只成功完成单位净值同步后维护事件和 adj 缺口；adj 返回失败或维护抛错都会进入 `nav_repair_queue`。
- `scripts/nav_repair_pass.py`：每次最多 50 条，处理到期的 pending/failed 任务；attempts 达到 3 会 WARNING。
- 脚本注释提供建议 cron（未修改系统 crontab）：

```cron
30 21 * * * cd /root/workspace/ifund/backend && ./venv/bin/python3.12 scripts/nav_repair_pass.py
```

## M6：统计、测试与实测

### 自动化结果

在 `backend` 目录执行：

```text
PYTHONPATH=. ./venv/bin/pytest -q
34 passed in 1.35s

./venv/bin/pylint app
Your code has been rated at 10.00/10
```

净值治理相关 app/CLI/脚本组合也实测为 10.00/10。`pyproject.toml` 的 `disable` 保持为空，必要例外使用源码内联/模块注释。

### 实际小样本

验证时间均在 20:00 前，基金数不超过 1，并发不超过 1：

1. NAV 回补：`nav backfill --codes 028460 --concurrency 1 --limit 1` 成功，写入 23 行；重复执行仍成功写入同 23 行，数据库没有重复业务键。
2. Tushare adj 路径：针对 `519981` 使用本地 fin-data 的 Tushare `fund_nav` 镜像进行 CLI 链路验证，写入 3,660 行；实际库行 `2026-08-27` 为 `nav=2.613`、`acc_nav=3.13`、`adj_nav=4.209546`、`adj_src=tushare`。
3. 事件路径：针对 `519981` 使用本地 fin-data 的 Tushare 分红镜像写入 5 个唯一事件（上游返回的重复 2019 事件由唯一键收敛）。
4. 自算/交叉检查：`028460` 无事件的 Tushare+calc 小样本抽取 20 行，`max_relative_error=0.0`、`warning=false`；代码级事件独立验算测试的最大误差小于 0.5%。

### 数据源风险与解释

本次直接 POST `http://tushare.pro` 返回 HTTP 200 但 Tushare `code=1`、消息为“服务异常，请稍后再试”，所以实际 CLI adj 验证采用同机 fin-data 服务中已经缓存的 Tushare 原始结果；生产代码默认仍是用户指定的直连端点。直连恢复后可直接运行：

```bash
cd /root/workspace/ifund/backend
PYTHONPATH=. ./venv/bin/python3.12 -m cli nav adj --codes 519981 --src both --concurrency 1 --limit 1
```

另外，`519981` 镜像中的历史 `adj_nav` 表现为累计/后复权方向：用本交付严格的最新日基准前复权事件公式抽样时最大相对误差约 61.1001%，引擎已按要求输出 WARNING，而没有伪报通过。这是 Tushare 字段口径与已确认公式之间的待确认风险；无事件的 028460 交叉检查为 0%，事件公式的合成测试通过。上线前应确认 Tushare `adj_nav` 的历史方向，必要时单独增加方向标识/转换策略。

## CLI 示例

```bash
cd /root/workspace/ifund/backend

# 默认只处理 nav_repair_queue 中到期 pending NAV 任务
PYTHONPATH=. ./venv/bin/python3.12 -m cli nav backfill --concurrency 4 --limit 50

# 小样本 NAV 全史回补
PYTHONPATH=. ./venv/bin/python3.12 -m cli nav backfill \
  --codes 028460,519981 --concurrency 2 --limit 2

# 按类型/全量（全量由后续 bgjob 调度，不要在验证阶段执行）
PYTHONPATH=. ./venv/bin/python3.12 -m cli nav backfill --types '货币型*' --all --concurrency 8

# Tushare、事件自算或两者
PYTHONPATH=. ./venv/bin/python3.12 -m cli nav adj --codes 519981 --src tushare --concurrency 1 --limit 1
PYTHONPATH=. ./venv/bin/python3.12 -m cli nav adj --codes 028460 --src calc --concurrency 1 --limit 1
PYTHONPATH=. ./venv/bin/python3.12 -m cli nav adj --codes 519981 --src both --concurrency 1 --limit 1
PYTHONPATH=. ./venv/bin/python3.12 -m cli nav adj --codes 028460 --src calc --only-null --concurrency 1 --limit 1

# 分红/拆分事件
PYTHONPATH=. ./venv/bin/python3.12 -m cli nav events --codes 519981 --concurrency 1 --limit 1
```

## 收尾审查记录

收尾审查日期：2026-08-30。

- 现场确认：治理新增文件全部存在且可导入/可编译；Git 未跟踪项为 `div_split_crud.py`、`repair_crud.py`、`adj_engine.py`、`backfill_worker.py`、`events_worker.py`、`tushare_client.py`、`cli/nav.py`、`scripts/nav_repair_pass.py`、`docs/nav_governance_delivery.md`。
- 无关 diff 逐文件复核结论：`cluster/algo/dedup.py`、`common/rate_limit.py`、`fund_holdings/fetch/__init__.py`、`fund_holdings/fetch/worker.py`、`fund_manager/api/router.py`、`historical/backtest.py`、`historical/perpetual_backtest.py`、`historical/quarter.py`、`historical/screen.py`、`perpetual/algo/loader.py`、`perpetual/algo/pipeline.py`、`perpetual/algo/replay.py`、`perpetual/api/router.py`、`position/algo/backtest.py`、`position/algo/pipeline.py`、`position/algo/recommend.py`、`reconcile/algo/reconcile.py`、`reconcile/api/router.py`、`reconcile/crud/holdings_compute.py`、`reconcile/crud/txn_store.py`、`routers/auth.py` 均仅为 lint 豁免、未使用参数/局部变量清理或导入整理；没有发现与净值治理无关的行为改动，因此不回退。
- `db/sqlite.py` 的新增表白名单、`fund/api/router.py` 的复权字段扩展、`main.py` 的迁移/统计、`daily_nav_sync.py` 的 repair 接入、`cli/__main__.py` 的 nav 子命令均属于本次治理范围；没有修改 `pyproject.toml` 的全局 pylint disable。
- 复权口径补强：Tushare 失败入队；交叉检查将可比、不可比、超过 0.5% 的 mismatch 分开统计并输出 WARNING；`nav adj --src calc --only-null` 只补空值，不覆盖已有复权值。

## 全量回补执行命令

以下命令只由父代理在 20:00–23:00 之外的维护窗口执行；本次收尾没有启动全量任务。均从 `backend` 目录运行，`N` 表示目标基金数，实际耗时以日志、网络重试和失败队列为准。

1. `nav backfill --all`：

   ```bash
   PYTHONPATH=. ./venv/bin/python3.12 -m cli nav backfill --all --concurrency 8
   ```

   建议并发 4–8；全局基金间请求间隔固定至少 0.3 秒，粗略下界为 `N×0.3` 秒，另加接口耗时/重试（每 1,000 只通常约 5–15 分钟）。

2. `nav adj --src tushare`：

   ```bash
   PYTHONPATH=. ./venv/bin/python3.12 -m cli nav adj --src tushare --all --concurrency 2
   ```

   建议并发 2–4，避免 Tushare 限流；每只基金 1 次请求，按网络情况每 1,000 只预留约 8–25 分钟。

3. `nav events --all`：

   ```bash
   PYTHONPATH=. ./venv/bin/python3.12 -m cli nav events --all --concurrency 2
   ```

   建议并发 2–4；每只基金包含分红和拆分两次接口调用，每 1,000 只预留约 15–40 分钟。

4. `nav adj --src calc --only-null`：

   ```bash
   PYTHONPATH=. ./venv/bin/python3.12 -m cli nav adj --src calc --only-null --all --concurrency 1
   ```

   建议并发 1–2，优先控制 SQLite 写锁竞争；每只基金按基金单独事务，仅写 `adj_nav IS NULL` 行，每 1,000 只预留约 3–20 分钟。

   四步之间建议按“`backfill` → Tushare adj → events → calc only-null”顺序执行；任一步失败由 `nav_repair_queue` 收口，夜间 repair pass 单次最多 50 条。任何批量任务都不要在 20:00–23:00 启动超过 50 只基金的批次。
