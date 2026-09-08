# AkShare rank 快照回填

`ifund nav rankbackfill` 使用 `ak.fund_open_fund_rank_em(symbol="全部")` 的默认行为：
在 spawn 子进程中单次取得全量开放基金 rank 快照。iFund 只为 AkShare 的请求补充
`(5, 15)` 连接/读取超时，不修改默认的 `pi/pn`，也不执行分页循环。

## 日期语义

rank 接口只提供调用时的最新净值快照，不提供历史截止参数。`--target-date YYYY-MM-DD`
因此是本地校验/过滤条件：只允许快照中日期等于目标日的行进入回填候选，其余行计入
`date_filtered` 和 `skipped`，并在日志与 JSON 的 `warnings` 中明确提示。它不能用来重建
历史快照；例如 8 月 31 日的历史缺口应由东财 F10 `/f10/lsjz` 增量通道补齐。

## 命令

先做只读统计（仍会联网获取一次全量快照，但不会写 `fund_nav`）：

```bash
cd /root/workspace/ifund/backend
venv/bin/python3.12 ifund_cli.py nav rankbackfill --dry-run --json
```

确认统计后执行正式回填：

```bash
cd /root/workspace/ifund/backend
venv/bin/python3.12 ifund_cli.py nav rankbackfill --json
```

如需只接受已知的最新净值日，可在上述命令增加 `--target-date YYYY-MM-DD`。不要把历史
日期传给该参数来期待历史回溯；不匹配行会被安全过滤，不会写入错误日期。
