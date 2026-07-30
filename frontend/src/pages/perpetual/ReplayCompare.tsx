import { Card, Col, Row, Statistic, Table, Tag, theme } from 'antd'
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { Backtest, TurnoverEntry } from './types'

interface Props {
  replay: Backtest
  buyhold: Backtest
  turnover: TurnoverEntry[]
}

export default function ReplayCompare({ replay, buyhold, turnover }: Props) {
  const { token } = theme.useToken()

  const merged = (() => {
    const bhMap = new Map(buyhold.curve.map((p) => [p.date, p.nav]))
    return replay.curve.map((p) => ({
      date: p.date,
      replay: +((p.nav - 1) * 100).toFixed(2),
      buyhold: bhMap.has(p.date) ? +(((bhMap.get(p.date) as number) - 1) * 100).toFixed(2) : null,
    }))
  })()

  const cols = [
    { title: '锚点', dataIndex: 'anchor', key: 'anchor', width: 110 },
    { title: '留存', dataIndex: 'kept', key: 'kept', width: 60 },
    {
      title: '换仓',
      key: 'swaps',
      render: (_: unknown, r: TurnoverEntry) =>
        r.swaps.length ? (
          <span style={{ fontSize: 12 }}>
            {r.swaps.map((s, i) => (
              <Tag key={i} color="orange" style={{ marginBottom: 2 }}>
                {s.out}→{s.in ?? '?'}
              </Tag>
            ))}
          </span>
        ) : (
          <span style={{ color: token.colorTextTertiary }}>—</span>
        ),
    },
    {
      title: '备注',
      dataIndex: 'note',
      key: 'note',
      render: (v: string | null) => v ? <Tag color="red">{v}</Tag> : null,
    },
  ]

  return (
    <Card title="重筛回放 vs 躺平基准" size="small">
      <Row gutter={16} style={{ marginBottom: 16 }}>
        {[
          { label: '重筛年化', val: replay.annual_return, color: token.colorPrimary },
          { label: '躺平年化', val: buyhold.annual_return, color: token.colorTextSecondary },
          { label: '重筛回撤', val: replay.max_drawdown, color: token.colorError },
          { label: '重筛夏普', val: replay.sharpe, color: token.colorSuccess },
        ].map((s) => (
          <Col span={6} key={s.label}>
            <Statistic
              title={s.label}
              value={s.label.includes('夏普') ? s.val : s.val * 100}
              precision={s.label.includes('夏普') ? 2 : 1}
              suffix={s.label.includes('夏普') ? '' : '%'}
              valueStyle={{ color: s.color, fontSize: 16 }}
            />
          </Col>
        ))}
      </Row>
      <ResponsiveContainer width="100%" height={260}>
        <LineChart data={merged} margin={{ top: 5, right: 20, bottom: 5, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={token.colorBorderSecondary} />
          <XAxis dataKey="date" tick={{ fontSize: 10 }} interval="preserveStartEnd" />
          <YAxis tick={{ fontSize: 10 }} tickFormatter={(v) => `${v}%`} />
          <Tooltip formatter={(v) => `${v}%`} />
          <Line type="monotone" dataKey="replay" name="重筛" stroke={token.colorPrimary} dot={false} strokeWidth={2} />
          <Line type="monotone" dataKey="buyhold" name="躺平" stroke={token.colorTextTertiary} dot={false} strokeDasharray="5 3" />
        </LineChart>
      </ResponsiveContainer>
      <Table
        dataSource={turnover}
        columns={cols}
        rowKey="anchor"
        size="small"
        pagination={false}
        style={{ marginTop: 12 }}
      />
    </Card>
  )
}
