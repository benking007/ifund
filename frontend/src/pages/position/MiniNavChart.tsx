import { theme } from 'antd'
import { Area, AreaChart, ResponsiveContainer, Tooltip, YAxis } from 'recharts'
import type { NavCurvePoint } from './types'

// 代表基金迷你走势图：净值已 rebase 到起点 1.0，展示累计收益率；
// 鼠标移上去显示当前日期与累计收益（涨红跌绿）。
export default function MiniNavChart({ data, height = 48 }: { data: NavCurvePoint[]; height?: number }) {
  const { token } = theme.useToken()
  if (!data || data.length < 2) {
    return <span style={{ fontSize: 12, color: token.colorTextTertiary }}>净值不足</span>
  }
  const last = data[data.length - 1].nav
  const up = last >= 1
  const color = up ? '#f5222d' : '#52c41a' // 涨红跌绿
  const gid = up ? 'miniUp' : 'miniDown'
  const curve = data.map((p) => ({ date: p.date, ret: +((p.nav - 1) * 100).toFixed(2) }))

  return (
    <ResponsiveContainer width="100%" height={height}>
      <AreaChart data={curve} margin={{ top: 2, right: 0, left: 0, bottom: 0 }}>
        <defs>
          <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity={0.3} />
            <stop offset="100%" stopColor={color} stopOpacity={0.02} />
          </linearGradient>
        </defs>
        <YAxis hide domain={['auto', 'auto']} />
        <Tooltip
          contentStyle={{
            background: token.colorBgElevated,
            border: `1px solid ${token.colorBorderSecondary}`,
            borderRadius: token.borderRadius,
            fontSize: 12,
            padding: '4px 8px',
          }}
          labelStyle={{ color: token.colorTextSecondary }}
          itemStyle={{ color: token.colorText }}
          labelFormatter={(d) => `${d}`}
          formatter={(v) => {
            const value = typeof v === 'number' ? v : Number(v ?? 0)
            return [`${value >= 0 ? '+' : ''}${value.toFixed(2)}%`, '累计收益']
          }}
        />
        <Area
          type="monotone"
          dataKey="ret"
          stroke={color}
          strokeWidth={1.3}
          fill={`url(#${gid})`}
          isAnimationActive={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  )
}
