"""永续组合冻结参数——一次标定即固定，不随重跑再优化。"""
from __future__ import annotations

# ── 硬门槛 ──
MIN_TENURE_DAYS = 1095
MIN_SCALE = 2.0
MAX_SCALE = 400.0
MIN_NAV_DAYS = 750
NAME_EXCLUDES = ("年", "月", "季")
TYPE_PREFIXES = ("混合型-偏股", "混合型-灵活", "混合型-股债平衡", "股票型")

# ── 质量分权重（风险调整，收益符号中立） ──
W_SHARPE_MED = 0.35
W_SHARPE_STA = 0.20
W_SORTINO = 0.25
W_TENURE = 0.10
W_DRIFT = 0.10

# ── 选择 / 权重 ──
TARGET_HOLDINGS = 10
LAMBDA_DIV = 0.6
WMAX = 0.20
MAX_PER_COMPANY = 2
N_STYLE_AXES = 4
MU_STYLE = 0.5

# ── 时间窗口 ──
ROLL = 252
NAV_START = "2021-01-01"
ALIGN_START = "2023-07-01"
MIN_COMMON_DAYS = 250
