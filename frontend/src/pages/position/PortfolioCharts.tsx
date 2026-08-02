import { useMemo, useState } from 'react'
import { Card, Col, Row, Segmented, Statistic, theme } from 'antd'
import dayjs from 'dayjs'
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { Portfolio, PortfolioPoint } from './types'

// 选中区间内重算视图：净值在窗口起点 rebase 到 1.0，逐日算回撤，
// 再按真实日期跨度年化得到收益/夏普——保证图与统计始终对应所选区间。
function buildView(points: PortfolioPoint[]) {
  const base = points[0].nav
  let peak = 0
  let maxDd = 0
  const curve = points.map((p) => {
    const nav = p.nav / base
    peak = Math.max(peak, nav)
    const dd = peak > 0 ? (nav - peak) / peak : 0
    maxDd = Math.min(maxDd, dd)
    // ret：相对窗口起点的累计收益率（%）；underwater 回撤另算
    return { date: p.date, ret: +((nav - 1) * 100).toFixed(2), drawdown: +(dd * 100).toFixed(2) }
  })

  const rets: number[] = []
  for (let i = 1; i < points.length; i++) rets.push(points[i].nav / points[i - 1].nav - 1)
  const mean = rets.length ? rets.reduce((a, b) => a + b, 0) / rets.length : 0
  const variance =
    rets.length > 1 ? rets.reduce((a, b) => a + (b - mean) ** 2, 0) / (rets.length - 1) : 0
  const std = Math.sqrt(variance)

  const spanDays = Math.max(
    1,
    dayjs(points[points.length - 1].date).diff(dayjs(points[0].date), 'day'),
  )
  const periodsPerYear = (rets.length * 365.25) / spanDays
  const ratio = points[points.length - 1].nav / base
  const totalReturn = ratio - 1
  const annualReturn = Math.pow(ratio, 365.25 / spanDays) - 1
  const sharpe = std > 0 ? (mean / std) * Math.sqrt(periodsPerYear) : 0

  return { curve, totalReturn, annualReturn, maxDrawdown: -maxDd, sharpe }
}

const RANGES = [
  { label: '近3月', value: 3 },
  { label: '近6月', value: 6 },
  { label: '近1年', value: 12 },
  { label: '近3年', value: 36 },
  { label: '近5年', value: 60 },
  { label: '今年以来', value: -1 },
  { label: '全部', value: 0 },
]

// 组合净值 + 回撤走势：净值面积图（窗口起点 rebase 到 1.0）叠加 underwater 回撤图。
// 颜色取自 antd 主题 token，明暗主题自适应；顶部区间可切换。
export default function PortfolioCharts({ portfolio }: { portfolio: Portfolio }) {
  const { token } = theme.useToken()
  const [range, setRange] = useState(0)
  const full = portfolio.curve

  const sliced = useMemo(() => {
    if (!range || full.length === 0) return full
    const end = full[full.length - 1].date
    const cutoff = range === -1
      ? dayjs(end).startOf('year')
      : dayjs(end).subtract(range, 'month')
    const win = full.filter((p) => !dayjs(p.date).isBefore(cutoff))
    return win.length >= 2 ? win : full
  }, [full, range])

  const view = useMemo(() => (sliced.length >= 2 ? buildView(sliced) : null), [sliced])
  if (!view) return null

  const { curve, totalReturn, annualReturn, maxDrawdown, sharpe } = view
  const span = `${curve[0].date} ~ ${curve[curve.length - 1].date}`
  const gain = (v: number) => (v >= 0 ? '#f5222d' : '#52c41a') // 涨红跌绿

  const fmtTick = (d: string) => `${d.slice(2, 4)}/${d.slice(5, 7)}`
  const tickGap = Math.max(1, Math.floor(curve.length / 8))

  const axisTick = { fontSize: 11, fill: token.colorTextTertiary }
  const tooltipStyle = {
    background: token.colorBgElevated,
    border: `1px solid ${token.colorBorderSecondary}`,
    borderRadius: token.borderRadius,
    fontSize: 12,
  }

  return (
    <Card
      size="small"
      title="组合表现"
      extra={
        <Segmented
          size="small"
          options={RANGES}
          value={range}
          onChange={(v) => setRange(v as number)}
        />
      }
    >
      <Row gutter={16} style={{ marginBottom: 4 }}>
        <Col span={6}>
          <Statistic
            title="累计收益"
            value={totalReturn * 100}
            precision={2}
            suffix="%"
            valueStyle={{ color: gain(totalReturn), fontSize: 20 }}
          />
        </Col>
        <Col span={6}>
          <Statistic
            title="年化收益"
            value={annualReturn * 100}
            precision={2}
            suffix="%"
            valueStyle={{ color: gain(annualReturn), fontSize: 20 }}
          />
        </Col>
        <Col span={6}>
          <Statistic
            title="最大回撤"
            value={-maxDrawdown * 100}
            precision={2}
            suffix="%"
            valueStyle={{ color: token.colorWarning, fontSize: 20 }}
          />
        </Col>
        <Col span={6}>
          <Statistic
            title="夏普比率"
            value={sharpe}
            precision={2}
            valueStyle={{ color: sharpe >= 1 ? '#f5222d' : token.colorText, fontSize: 20 }}
          />
        </Col>
      </Row>
      <div style={{ fontSize: 12, color: token.colorTextTertiary, marginBottom: 8 }}>
        {span} · 按目标权重回测，夏普以 rf=0 计
      </div>

      <div style={{ fontSize: 12, color: token.colorTextSecondary, margin: '4px 0' }}>收益率走势</div>
      <ResponsiveContainer width="100%" height={220}>
        <AreaChart data={curve} margin={{ top: 8, right: 12, left: 0, bottom: 0 }} syncId="portfolio">
          <defs>
            <linearGradient id="retFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={token.colorPrimary} stopOpacity={0.32} />
              <stop offset="100%" stopColor={token.colorPrimary} stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke={token.colorBorderSecondary} />
          <XAxis dataKey="date" tickFormatter={fmtTick} interval={tickGap} tick={axisTick} minTickGap={16} />
          <YAxis domain={['auto', 'auto']} tickFormatter={(v) => `${v}%`} width={48} tick={axisTick} />
          <Tooltip
            labelFormatter={(d) => `日期 ${d}`}
            formatter={(v) => {
              const value = typeof v === 'number' ? v : Number(v ?? 0)
              return [`${value.toFixed(2)}%`, '累计收益率']
            }}
            contentStyle={tooltipStyle}
            labelStyle={{ color: token.colorTextSecondary }}
            itemStyle={{ color: token.colorText }}
          />
          <Area type="monotone" dataKey="ret" stroke={token.colorPrimary} strokeWidth={1.6} fill="url(#retFill)" isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>

      <div style={{ fontSize: 12, color: token.colorTextSecondary, margin: '8px 0 4px' }}>
        回撤走势（underwater）
      </div>
      <ResponsiveContainer width="100%" height={150}>
        <AreaChart data={curve} margin={{ top: 4, right: 12, left: 0, bottom: 0 }} syncId="portfolio">
          <defs>
            <linearGradient id="ddFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={token.colorError} stopOpacity={0.05} />
              <stop offset="100%" stopColor={token.colorError} stopOpacity={0.32} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke={token.colorBorderSecondary} />
          <XAxis dataKey="date" tickFormatter={fmtTick} interval={tickGap} tick={axisTick} minTickGap={16} />
          <YAxis domain={['auto', 0]} tickFormatter={(v) => `${v}%`} width={48} tick={axisTick} />
          <Tooltip
            labelFormatter={(d) => `日期 ${d}`}
            formatter={(v) => {
              const value = typeof v === 'number' ? v : Number(v ?? 0)
              return [`${value.toFixed(2)}%`, '回撤']
            }}
            contentStyle={tooltipStyle}
            labelStyle={{ color: token.colorTextSecondary }}
            itemStyle={{ color: token.colorText }}
          />
          <Area type="monotone" dataKey="drawdown" stroke={token.colorError} strokeWidth={1.2} fill="url(#ddFill)" isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
    </Card>
  )
}
