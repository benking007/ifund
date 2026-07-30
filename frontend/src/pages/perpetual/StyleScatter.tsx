import { useMemo } from 'react'
import { Card, Tag, theme } from 'antd'
import {
  CartesianGrid,
  LabelList,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from 'recharts'
import type { CloudPoint } from './types'

export default function StyleScatter({ cloud, onOpenDetail }: { cloud: CloudPoint[]; onOpenDetail?: (code: string) => void }) {
  const { token } = theme.useToken()
  const { bg, selected } = useMemo(() => {
    const bgPts = cloud.filter((c) => !c.selected)
    const selPts = cloud.filter((c) => c.selected)
    return { bg: bgPts, selected: selPts }
  }, [cloud])

  if (!cloud.length) return null

  return (
    <Card
      title="风格坐标（剥离市场 beta 后数据自带的主成分轴，非人工标签）"
      size="small"
    >
      <ResponsiveContainer width="100%" height={380}>
        <ScatterChart margin={{ top: 10, right: 30, bottom: 20, left: 10 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={token.colorBorderSecondary} />
          <XAxis
            type="number"
            dataKey="pc2"
            name="PC2"
            tick={{ fontSize: 11 }}
            label={{ value: 'PC2', position: 'bottom', fontSize: 11 }}
          />
          <YAxis type="number" dataKey="pc3" name="PC3" tick={{ fontSize: 11 }}
            label={{ value: 'PC3', angle: -90, position: 'insideLeft', fontSize: 11 }} />
          <ZAxis type="number" dataKey="q01" range={[30, 100]} />
          <Tooltip
            content={({ payload }) => {
              if (!payload?.length) return null
              const p = payload[0].payload as CloudPoint
              return (
                <div style={{ background: token.colorBgElevated, padding: 8, borderRadius: 6, fontSize: 12 }}>
                  <div><strong>{p.name}</strong> ({p.code})</div>
                  <div>PC2: {p.pc2.toFixed(2)} PC3: {p.pc3.toFixed(2)}</div>
                  <div>质量分位: {p.q01.toFixed(3)}</div>
                  {p.selected && <Tag color="blue" style={{ marginTop: 4 }}>入选</Tag>}
                </div>
              )
            }}
          />
          <Scatter data={bg} fill={token.colorTextQuaternary} fillOpacity={0.35}
            cursor={onOpenDetail ? 'pointer' : undefined}
            onClick={(d) => onOpenDetail?.((d as unknown as { payload: CloudPoint }).payload.code)} />
          <Scatter data={selected} fill={token.colorPrimary}
            cursor={onOpenDetail ? 'pointer' : undefined}
            onClick={(d) => onOpenDetail?.((d as unknown as { payload: CloudPoint }).payload.code)}>
            <LabelList
              dataKey="name"
              position="right"
              style={{ fontSize: 10, fill: token.colorTextSecondary }}
              formatter={(v) => (String(v).length > 8 ? `${String(v).slice(0, 8)}…` : String(v))}
            />
          </Scatter>
        </ScatterChart>
      </ResponsiveContainer>
      <div style={{ fontSize: 12, color: token.colorTextSecondary, marginTop: 4 }}>
        灰点=候选池 · 蓝点=入选（带名称标签） · 点大小=质量分位
      </div>
    </Card>
  )
}
