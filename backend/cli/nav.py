"""净值治理 CLI：回补、前复权、分红/拆分事件。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from app.fund_nav.fetch import adj_engine, backfill_worker, events_worker, rank_backfill

from . import helpers, output


def _codes(args, resolver) -> list[str]:
    return resolver(
        helpers.csv_list(args.codes),
        helpers.csv_list(args.types),
        all_requested=args.all,
        limit=args.limit,
    )


def cmd_backfill(args) -> None:
    """执行 nav 全史回补；无 --all 时只处理 pending 队列。"""
    result = backfill_worker.run_backfill(
        helpers.csv_list(args.codes),
        helpers.csv_list(args.types),
        all_requested=args.all,
        concurrency=args.concurrency,
        limit=args.limit,
    )
    output.emit(
        result,
        args.json,
        lambda data: print(
            f"✓ NAV 回补：目标{data['total']} 成功{data['success']} "
            f"失败{data['fail']} 写入{data['rows']} 行",
        ),
    )


def cmd_rankbackfill(args) -> None:
    """用 AkShare 全市场 rank 快照补齐本地最新日期缺口。"""
    result = rank_backfill.run_rank_backfill(
        dry_run=args.dry_run,
        limit=args.limit,
        target_date=args.target_date,
    )
    output.emit(
        result,
        args.json,
        lambda data: print(
            f"{'△' if data['dry_run'] else '✓'} rank 快照：目标{data['total']} "
            f"快照{data['snapshot_rows']}调用{data['api_calls']}次 "
            f"日期过滤{data['date_filtered']} "
            f"已补{data['backfilled']}待补{data['would_backfill']} "
            f"跳过{data['skipped']}无净值{data['no_nav']}失败{data['failed']}"
        ),
    )
    code = rank_backfill.exit_code(result)
    if code:
        raise SystemExit(code)


def _adj_one(code: str, source: str, only_null: bool = False) -> dict:
    """处理单只基金的 Tushare/calc/both 流程。"""
    result = {"code": code, "rows": 0, "cross_check": None}
    if source in {"tushare", "both"}:
        fetched = adj_engine.fetch_adj_tushare([code])
        result["rows"] += int(fetched.get("rows") or 0)
        if fetched.get("failed"):
            raise RuntimeError(f"Tushare adj_nav 失败: {fetched['failed']}")
    if source == "both":
        result["cross_check"] = adj_engine.cross_check(code)
    if source in {"calc", "both"}:
        calculated = adj_engine.calc_adj_from_events(code, only_null=only_null)
        result["rows"] += int(calculated.get("rows") or 0)
    result["validation_warning"] = bool(
        result.get("cross_check") and result["cross_check"].get("warning")
    )
    return result


def cmd_adj(args) -> None:
    """执行 Tushare adj_nav、自算或两者交叉验证。"""
    targets = backfill_worker.resolve_codes(
        helpers.csv_list(args.codes),
        helpers.csv_list(args.types),
        all_requested=args.all,
        limit=args.limit,
        task_kind="adj",
    )
    backfill_worker.ensure_batch_allowed(len(targets))
    success = fail = rows = warning_count = 0
    failures = []
    checks = []
    only_null = getattr(args, "only_null", False)
    if targets:
        completed = 0
        with ThreadPoolExecutor(max_workers=min(64, max(1, args.concurrency)),
                                thread_name_prefix="nav-adj") as executor:
            futures = {
                executor.submit(_adj_one, code, args.src, only_null): code
                for code in targets
            }
            for future in as_completed(futures):
                code = futures[future]
                try:
                    result = future.result()
                    success += 1
                    rows += result["rows"]
                    if result.get("cross_check") is not None:
                        checks.append(result["cross_check"])
                    if result.get("validation_warning"):
                        success -= 1
                        fail += 1
                        warning_count += 1
                        failures.append({
                            "code": code,
                            "error": "cross_check_warning",
                            "cross_check": result["cross_check"],
                        })
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    fail += 1
                    failures.append({"code": code, "error": str(exc)})
                completed += 1
                if completed % backfill_worker.CHECKPOINT_EVERY == 0:
                    backfill_worker.checkpoint_wal()
        if completed and completed % backfill_worker.CHECKPOINT_EVERY:
            backfill_worker.checkpoint_wal()
    result = {
        "total": len(targets), "success": success, "fail": fail,
        "rows": rows, "warnings": warning_count,
        "cross_checks": checks, "failures": failures[:20],
    }
    output.emit(
        result,
        args.json,
        lambda data: print(
            f"✓ adj_nav：目标{data['total']} 成功{data['success']} "
            f"失败{data['fail']} 写入{data['rows']} 行",
        ),
    )


def cmd_events(args) -> None:
    """采集 Tushare 分红/拆分事件。"""
    targets = _codes(args, events_worker.resolve_codes)
    result = events_worker.run_events(
        targets,
        [],
        all_requested=False,
        concurrency=args.concurrency,
        limit=None,
    )
    output.emit(
        result,
        args.json,
        lambda data: print(
            f"✓ 事件采集：目标{data['total']} 成功{data['success']} "
            f"失败{data['fail']} 写入{data['rows']} 行",
        ),
    )
