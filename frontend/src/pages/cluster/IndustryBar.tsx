import { Tooltip, theme } from 'antd'
import type { ClusterIndustry } from './types'

// 簇内 top 行业占比条形（按占比归一到最大值，横向比例条）
export default function IndustryBar({ items }: { items: ClusterIndustry[] }) {
  const { token } = theme.useToken()
  if (!items.length) return null
  const max = Math.max(...items.map((i) => i.ratio), 1)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      {items.map((it) => (
        <div key={it.label} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ width: 140, fontSize: 12, textAlign: 'right' }} title={it.label}>
            {it.label}
          </span>
          <div style={{ flex: 1, background: token.colorFillSecondary, borderRadius: 3, height: 14 }}>
            <Tooltip title={`平均持仓 ${it.ratio.toFixed(2)}%`}>
              <div
                style={{
                  width: `${(it.ratio / max) * 100}%`,
                  background: token.colorPrimary,
                  height: '100%',
                  borderRadius: 3,
                }}
              />
            </Tooltip>
          </div>
          <span style={{ width: 48, fontSize: 12 }}>{it.ratio.toFixed(2)}%</span>
        </div>
      ))}
    </div>
  )
}
