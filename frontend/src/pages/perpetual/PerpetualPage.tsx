import { useEffect, useMemo, useState } from 'react'
import {
  Alert, Button, Card, Col, DatePicker, Progress, Row, Select, Space,
  Statistic, Table, Tag, message, theme,
} from 'antd'
import { ThunderboltOutlined, HistoryOutlined, InfoCircleOutlined } from '@ant-design/icons'
import dayjs from 'dayjs'
import request from '../../api/request'
import PortfolioCharts from '../position/PortfolioCharts'
import FundDetailModal from '../fund/components/FundDetailModal'
import AlgoExplainer from './AlgoExplainer'
import StyleScatter from './StyleScatter'
import ReplayCompare from './ReplayCompare'
import type { PerpetualResult, ReplayResult } from './types'

interface Preset { id: number; name: string }

function periodReturn(curve: { date: string; nav: number }[], years: number): number | null {
  if (!curve.length) return null
  const end = curve[curve.length - 1]
  const cutoff = dayjs(end.date).subtract(years, 'year')
  const start = curve.find((p) => dayjs(p.date).isAfter(cutoff))
  if (!start) return null
  return (end.nav / start.nav - 1) * 100
}

function ytdReturn(curve: { date: string; nav: number }[]): number | null {
  if (!curve.length) return null
  const end = curve[curve.length - 1]
  const yearStart = dayjs(end.date).startOf('year')
  const start = curve.find((p) => !dayjs(p.date).isBefore(yearStart))
  if (!start) return null
  return (end.nav / start.nav - 1) * 100
}

export default function PerpetualPage() {
  const { token } = theme.useToken()
  const [loading, setLoading] = useState(false)
  const [replayLoading, setReplayLoading] = useState(false)
  const [result, setResult] = useState<PerpetualResult | null>(null)
  const [replayResult, setReplayResult] = useState<ReplayResult | null>(null)
  const [presets, setPresets] = useState<Preset[]>([])
  const [presetId, setPresetId] = useState<number | undefined>(() => {
    const v = localStorage.getItem('perpetual_preset')
    return v ? +v : undefined
  })
  const [asOf, setAsOf] = useState<string | undefined>()
  const [replayStart, setReplayStart] = useState<string>('2024-01-01')
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [detailCode, setDetailCode] = useState<string | null>(null)
  const [savedAt, setSavedAt] = useState<string | null>(null)

  useEffect(() => {
    request.get('/fund/presets')
      .then(({ data }) => setPresets(data.items ?? data ?? []))
      .catch(() => undefined)
    request.get('/perpetual/latest')
      .then(({ data }) => {
        if (data && data.result) {
          setResult(data.result)
          setSavedAt(data.created_at)
          if (data.preset_id) setPresetId(data.preset_id)
        }
      })
      .catch(() => undefined)
  }, [])

  const onPresetChange = (v: number) => {
    setPresetId(v)
    localStorage.setItem('perpetual_preset', String(v))
  }

  const perf = useMemo(() => {
    if (!result?.backtest.curve.length) return null
    const curve = result.backtest.curve
    return {
      ytd: ytdReturn(curve),
      y1: periodReturn(curve, 1),
      y3: periodReturn(curve, 3),
      y5: periodReturn(curve, 5),
    }
  }, [result])

  const runPerpetual = async () => {
    setLoading(true)
    setResult(null)
    try {
      const body: Record<string, unknown> = {}
      if (presetId) body.preset_id = presetId
      if (asOf) body.as_of = asOf
      const res = await request.post('/perpetual/run', body, { timeout: 300000 })
      if (res.data.error) {
        message.error(res.data.error)
      } else {
        setResult(res.data)
        request.post('/perpetual/save', { result: res.data, preset_id: presetId, as_of: asOf })
          .then(() => setSavedAt(new Date().toISOString().slice(0, 19).replace('T', ' ')))
          .catch(() => undefined)
      }
    } catch (e: unknown) {
      const apiErr = (e as { response?: { data?: { error?: string } } }).response?.data?.error
      message.error(apiErr ? `请求失败: ${apiErr}` : `请求失败: ${(e as Error).message}`)
    } finally {
      setLoading(false)
    }
  }

  const runReplay = async () => {
    setReplayLoading(true)
    setReplayResult(null)
    try {
      const body: Record<string, unknown> = { start: replayStart }
      if (presetId) body.preset_id = presetId
      const res = await request.post('/perpetual/replay', body, { timeout: 600000 })
      if (res.data.error) {
        message.error(res.data.error)
      } else {
        setReplayResult(res.data)
      }
    } catch (e: unknown) {
      const apiErr = (e as { response?: { data?: { error?: string } } }).response?.data?.error
      message.error(apiErr ? `回放失败: ${apiErr}` : `回放失败: ${(e as Error).message}`)
    } finally {
      setReplayLoading(false)
    }
  }

  const columns = [
    {
      title: '基金', dataIndex: 'name', key: 'name', width: 160, ellipsis: true,
      render: (v: string, r: { code: string }) => <a onClick={() => setDetailCode(r.code)}>{v}</a>,
    },
    { title: '代码', dataIndex: 'code', key: 'code', width: 70 },
    {
      title: '权重', dataIndex: 'weight', key: 'weight', width: 120,
      render: (v: number) => {
        const maxW = result ? Math.max(...result.holdings.map((h) => h.weight)) : 1
        return <Progress percent={+((v / maxW) * 100).toFixed(1)} format={() => `${(v * 100).toFixed(1)}%`} size="small" />
      },
    },
    { title: '质量', dataIndex: 'quality', key: 'quality', width: 70, render: (v: number) => v.toFixed(3) },
    { title: '公司', dataIndex: 'company', key: 'company', width: 90, ellipsis: true },
    {
      title: '股票仓', dataIndex: 'position_stock', key: 'pos', width: 70,
      render: (v: number | null) => v != null ? `${v.toFixed(0)}%` : '-',
    },
    {
      title: 'YTD', dataIndex: 'ytd', key: 'ytd', width: 70,
      render: (v: number | null) => v != null ? `${v.toFixed(1)}%` : '-',
    },
    { title: '任期(年)', dataIndex: 'tenure_years', key: 'tenure', width: 70 },
    {
      title: '夏普中', dataIndex: 'sharpe_med', key: 'sharpe', width: 70,
      render: (v: number | null) => v != null ? v.toFixed(3) : '-',
    },
    {
      title: 'PC2/PC3', key: 'style', width: 100,
      render: (_: unknown, r: { style_axes: number[] }) =>
        r.style_axes ? `${r.style_axes[0]?.toFixed(2)} / ${r.style_axes[1]?.toFixed(2)}` : '-',
    },
  ]

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Card size="small">
        <Space wrap>
          <Button type="primary" icon={<ThunderboltOutlined />} loading={loading} onClick={runPerpetual}>
            生成永续组合
          </Button>
          <Select
            placeholder="选择预设" style={{ minWidth: 200 }}
            value={presetId} onChange={onPresetChange}
            options={presets.map((p) => ({ label: p.name, value: p.id }))}
          />
          <DatePicker
            placeholder="决策日T(可选)"
            onChange={(_, ds) => setAsOf(Array.isArray(ds) ? ds[0] || undefined : ds || undefined)}
          />
          <DatePicker
            placeholder="回放起点"
            defaultValue={dayjs('2024-01-01')}
            onChange={(_, ds) => setReplayStart(Array.isArray(ds) ? ds[0] || '2024-01-01' : ds || '2024-01-01')}
          />
          <Button icon={<HistoryOutlined />} loading={replayLoading} onClick={runReplay}>
            重筛回放
          </Button>
          <Button icon={<InfoCircleOutlined />} onClick={() => setDrawerOpen(true)}>
            算法详解
          </Button>
        </Space>
      </Card>

      {result && (
        <>
          <Alert
            type="info" showIcon
            message={`候选 ${result.stats.universe} → 过门 ${result.stats.passed_gate} → 打分 ${result.stats.scored} → 去重 -${result.stats.dedup_removed} → 对齐 ${result.stats.aligned_pool}（${result.stats.common_days} 交易日） · PC1 方差 ${(result.meta.pc1_var_ratio * 100).toFixed(1)}% · λ=${result.meta.lambda_div} μ=${result.meta.mu_style} wmax=${result.meta.wmax}`}
          />
          <Card title={`永续组合持仓（${result.meta.n_selected} 只）`} size="small"
            extra={savedAt ? <span style={{ fontSize: 12, color: token.colorTextTertiary }}>保存于 {savedAt}</span> : undefined}>
            <Table
              dataSource={result.holdings} columns={columns} rowKey="code"
              size="small" pagination={false}
            />
          </Card>

          {result.backtest.curve.length > 0 && (
            <>
              <Card size="small">
                <Row gutter={16}>
                  <Col span={6}>
                    <Statistic title="累计收益" value={((result.backtest.curve[result.backtest.curve.length - 1].nav - 1) * 100)} precision={2} suffix="%"
                      valueStyle={{ color: token.colorPrimary }} />
                  </Col>
                  <Col span={6}>
                    <Statistic title="年化收益" value={result.backtest.annual_return * 100} precision={2} suffix="%"
                      valueStyle={{ color: token.colorSuccess }} />
                  </Col>
                  <Col span={6}>
                    <Statistic title="最大回撤" value={result.backtest.max_drawdown * 100} precision={2} suffix="%"
                      valueStyle={{ color: token.colorError }} />
                  </Col>
                  <Col span={6}>
                    <Statistic title="夏普比率" value={result.backtest.sharpe} precision={2} />
                  </Col>
                </Row>
                {perf && (
                  <Row gutter={16} style={{ marginTop: 12 }}>
                    <Col span={6}>
                      <Statistic title="今年以来" value={perf.ytd ?? '-'} precision={2} suffix={perf.ytd != null ? '%' : ''}
                        valueStyle={perf.ytd != null ? { color: perf.ytd >= 0 ? token.colorSuccess : token.colorError } : undefined} />
                    </Col>
                    <Col span={6}>
                      <Statistic title="近一年" value={perf.y1 ?? '-'} precision={2} suffix={perf.y1 != null ? '%' : ''}
                        valueStyle={perf.y1 != null ? { color: perf.y1 >= 0 ? token.colorSuccess : token.colorError } : undefined} />
                    </Col>
                    <Col span={6}>
                      <Statistic title="近三年" value={perf.y3 ?? '-'} precision={2} suffix={perf.y3 != null ? '%' : ''}
                        valueStyle={perf.y3 != null ? { color: perf.y3 >= 0 ? token.colorSuccess : token.colorError } : undefined} />
                    </Col>
                    <Col span={6}>
                      <Statistic title="近五年" value={perf.y5 ?? '-'} precision={2} suffix={perf.y5 != null ? '%' : ''}
                        valueStyle={perf.y5 != null ? { color: perf.y5 >= 0 ? token.colorSuccess : token.colorError } : undefined} />
                    </Col>
                  </Row>
                )}
              </Card>
              <PortfolioCharts portfolio={{
                curve: result.backtest.curve.map((p) => ({ date: p.date, nav: p.nav, drawdown: p.drawdown })),
                max_drawdown: result.backtest.max_drawdown,
                annual_return: result.backtest.annual_return,
                annual_vol: result.backtest.annual_vol,
                sharpe: result.backtest.sharpe,
              }} />
            </>
          )}

          <Row gutter={16}>
            <Col span={12}>
              <Card size="small" title="分散度">
                <Row gutter={8}>
                  <Col span={8}>
                    <Statistic title="原始相关均值" value={result.diversification.orig_corr_mean} precision={3} />
                  </Col>
                  <Col span={8}>
                    <Statistic title="残差相关均值" value={result.diversification.resid_corr_mean} precision={3}
                      valueStyle={{ color: token.colorSuccess }} />
                  </Col>
                  <Col span={8}>
                    <Statistic title="ENB" value={result.diversification.enb} precision={2}
                      suffix={`/ ${result.diversification.enb_target}`} />
                  </Col>
                </Row>
              </Card>
            </Col>
            <Col span={12}>
              <Card size="small" title="风格轴方差占比">
                <Space>
                  <Tag>PC1(β) {(result.meta.pc1_var_ratio * 100).toFixed(1)}%</Tag>
                  {result.meta.style_axis_var.map((v, i) => (
                    <Tag key={i}>PC{i + 2} {(v * 100).toFixed(1)}%</Tag>
                  ))}
                </Space>
              </Card>
            </Col>
          </Row>

          {result.cloud && result.cloud.length > 0 && <StyleScatter cloud={result.cloud} onOpenDetail={setDetailCode} />}
        </>
      )}

      {replayResult && (
        <ReplayCompare
          replay={replayResult.replay}
          buyhold={replayResult.buyhold}
          turnover={replayResult.turnover}
        />
      )}

      <AlgoExplainer open={drawerOpen} onClose={() => setDrawerOpen(false)} />
      <FundDetailModal code={detailCode} open={detailCode !== null} onClose={() => setDetailCode(null)} />
    </Space>
  )
}
