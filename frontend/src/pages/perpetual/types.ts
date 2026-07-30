export interface PerpetualHolding {
  code: string
  name: string
  company: string
  weight: number
  quality: number
  position_stock: number | null
  ytd: number | null
  tenure_years: number
  sharpe_med: number | null
  style_axes: number[]
}

export interface PerpetualStats {
  universe: number
  passed_gate: number
  scored: number
  dedup_removed: number
  aligned_pool: number
  common_days: number
}

export interface PerpetualMeta {
  target_holdings: number
  n_selected: number
  align_start: string
  nav_as_of: string
  pc1_var_ratio: number
  style_axis_var: number[]
  lambda_div: number
  mu_style: number
  wmax: number
  as_of: string | null
}

export interface Diversification {
  orig_corr_mean: number
  resid_corr_mean: number
  enb: number
  enb_target: number
}

export interface BacktestPoint {
  date: string
  nav: number
  drawdown: number
}

export interface Backtest {
  curve: BacktestPoint[]
  max_drawdown: number
  annual_return: number
  annual_vol: number
  sharpe: number
}

export interface Diagnostic {
  code: string
  name?: string
  in_pool: boolean
  selected: boolean
  q01?: number
  style_axes?: number[]
  avg_corr_to_selected?: number
  greedy_score?: number
  weakest_selected?: { code: string; name: string; q01: number }
}

export interface CloudPoint {
  code: string
  name: string
  pc2: number
  pc3: number
  q01: number
  selected: boolean
}

export interface PerpetualResult {
  stats: PerpetualStats
  meta: PerpetualMeta
  holdings: PerpetualHolding[]
  diversification: Diversification
  backtest: Backtest
  diagnostics?: Diagnostic[]
  cloud?: CloudPoint[]
  error?: string
}

export interface ReplaySwap {
  out: string
  in: string | null
}

export interface TurnoverEntry {
  anchor: string
  kept: number
  swaps: ReplaySwap[]
  note: string | null
}

export interface ReplayResult {
  meta: {
    start: string
    step_months: number
    keep_rank: number
    max_replace: number
    anchors: string[]
  }
  replay: Backtest
  buyhold: Backtest
  turnover: TurnoverEntry[]
  error?: string
}
