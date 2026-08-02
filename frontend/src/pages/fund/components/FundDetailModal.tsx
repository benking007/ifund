import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Button, Descriptions, Empty, Modal, Segmented, Select, Space, Spin, Statistic, Table, Tabs, Tag, Typography, theme } from 'antd'
import dayjs from 'dayjs'
import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import request from '../../../api/request'
import rawFetch from '../../../api/rawFetch'
import { APP_BASE } from '../../../config'
import type { HoldingItem } from '../types'
import {
  CONC_META, CONFIDENCE_META, LUCK_META, SCALE_RISK_META, STYLE_META, metaOf, parseTags,
} from '../aiMeta'
import type { FundAi } from '../aiMeta'

interface NavPoint {
  date: string
  nav: number
}

const NAV_RANGES = [
  { label: '近3月', value: 3 },
  { label: '近6月', value: 6 },
  { label: '近1年', value: 12 },
  { label: '近3年', value: 36 },
  { label: '近5年', value: 60 },
  { label: '今年以来', value: -1 },
  { label: '全部', value: 0 },
]

const UP = '#f5222d'
const DOWN = '#52c41a'

interface Props {
  code: string | null
  open: boolean
  onClose: () => void
}

interface DetailData {
  [key: string]: unknown
  holdings?: HoldingItem[]
}

const BASIC_FIELDS: [string, string][] = [
  ['fund_name', '基金简称'],
  ['fund_full_name', '基金全称'],
  ['fund_company', '基金公司'],
  ['fund_manager', '基金经理'],
  ['establish_date', '成立日期'],
  ['scale', '规模'],
  ['fund_type', '类型'],
  ['fund_rating', '评级'],
]

const PERF_FIELDS: [string, string][] = [
  ['return_ytd', '今年以来'],
  ['return_1y', '近一年'],
  ['return_3y', '近三年'],
  ['return_5y', '近五年'],
  ['sharpe_3y', '夏普3年'],
  ['max_drawdown_3y', '最大回撤3年'],
  ['position_stock', '股票仓位'],
  ['position_bond', '债券仓位'],
]

function fmt(v: unknown): string {
  if (v === null || v === undefined || v === '') return '-'
  return String(v)
}

function waitFor(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms)
    const onAbort = () => {
      clearTimeout(timer)
      reject(new DOMException('Aborted', 'AbortError'))
    }
    if (signal.aborted) onAbort()
    else signal.addEventListener('abort', onAbort, { once: true })
  })
}

function enumTag(map: Record<string, { label: string; color: string }>, v: string | null | undefined) {
  const m = metaOf(map, v)
  return m ? <Tag color={m.color}>{m.label}</Tag> : <span className="text-gray-400">-</span>
}

function boolTag(v: number | null | undefined, yes: string, no: string) {
  if (v === null || v === undefined) return <span className="text-gray-400">-</span>
  return <Tag color={v ? 'blue' : 'default'}>{v ? yes : no}</Tag>
}

/** AI 定性分析面板：未分析时给出 CLI 填充提示。 */
function renderAiPanel(ai: FundAi | null | undefined, onRunAi: () => void, aiLoading: boolean) {
  if (!ai) {
    return (
      <Empty description={
          <div>
            <div style={{ marginBottom: 12, color: 'var(--text-soft, #999)' }}>
              {'暂无 AI 定性分析（也可通过 CLI 填充：preset ai-set --code 代码 --data ...）'}
            </div>
            <Button type="primary" size="small" loading={aiLoading} onClick={onRunAi}>
              AI 定性分析
            </Button>
          </div>
        } />
    )
  }
  const stars = ai.rating != null ? '★'.repeat(Math.max(0, Math.min(3, ai.rating))) || '·' : '-'
  const tags = parseTags(ai.tags)
  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Space wrap align="center">
        <span style={{ color: '#fadb14', fontSize: 16, letterSpacing: 2 }}>{stars}</span>
        {ai.recommend ? <Tag color="green">推荐</Tag> : null}
        {enumTag(CONFIDENCE_META, ai.confidence) /* 把握度 */}
        <Typography.Text strong>{fmt(ai.verdict)}</Typography.Text>
        <Button size="small" loading={aiLoading} onClick={onRunAi}>重新分析</Button>
      </Space>

      <Descriptions size="small" column={2} bordered title="核心维度">
        <Descriptions.Item label="实力分">
          {ai.skill_score != null ? `${ai.skill_score} / 100` : '-'}
        </Descriptions.Item>
        <Descriptions.Item label="运气 vs 实力">{enumTag(LUCK_META, ai.luck_verdict)}</Descriptions.Item>
        <Descriptions.Item label="选股/择时理由" span={2}>{fmt(ai.skill_reason)}</Descriptions.Item>
        <Descriptions.Item label="集中度">{enumTag(CONC_META, ai.concentration)}</Descriptions.Item>
        <Descriptions.Item label="集中度理由">{fmt(ai.concentration_reason)}</Descriptions.Item>
        <Descriptions.Item label="硬实力逻辑" span={2}>{fmt(ai.hard_thesis)}</Descriptions.Item>
      </Descriptions>

      <Descriptions size="small" column={2} bordered title="经理 / 风险锚点">
        <Descriptions.Item label="基金经理">{fmt(ai.manager)}</Descriptions.Item>
        <Descriptions.Item label="任职年限">
          {ai.tenure_years != null ? `${ai.tenure_years} 年` : '-'}
        </Descriptions.Item>
        <Descriptions.Item label="是否原装">{boolTag(ai.is_original, '原装', '非原装')}</Descriptions.Item>
        <Descriptions.Item label="是否共管">{boolTag(ai.is_comanaged, '共管', '独管')}</Descriptions.Item>
        <Descriptions.Item label="规模风险">{enumTag(SCALE_RISK_META, ai.scale_risk)}</Descriptions.Item>
        <Descriptions.Item label="风格稳定性">{enumTag(STYLE_META, ai.style_stability)}</Descriptions.Item>
        <Descriptions.Item label="换手备注" span={2}>{fmt(ai.turnover_note)}</Descriptions.Item>
      </Descriptions>

      {tags.length > 0 && (
        <Space wrap>
          {tags.map((t) => (
            <Tag key={t}>{t}</Tag>
          ))}
        </Space>
      )}

      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        出处：{fmt(ai.model)} · 依据 {fmt(ai.data_basis)} · 分析 {fmt(ai.analyzed_at)} · 更新 {fmt(ai.updated_at)}
      </Typography.Text>
    </Space>
  )
}

function NavChart({ code, enabled }: { code: string; enabled: boolean }) {
  const { token } = theme.useToken()
  const [data, setData] = useState<NavPoint[]>([])
  const [loading, setLoading] = useState(false)
  const [range, setRange] = useState(12)
  const [active, setActive] = useState<NavPoint | null>(null)
  const [navLoading, setNavLoading] = useState(false)
  const navSyncInFlight = useRef(false)
  const navAbortRef = useRef<AbortController | null>(null)

  const triggerNavSync = useCallback(async () => {
    if (!code || navSyncInFlight.current) return
    navSyncInFlight.current = true
    setNavLoading(true)
    navAbortRef.current?.abort()
    const controller = new AbortController()
    navAbortRef.current = controller
    try {
      const resp = await rawFetch(`${APP_BASE}/api/fund_nav/sync`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ codes: [code] }),
        signal: controller.signal,
      })
      if (!resp.ok) {
        const e = await resp.json().catch(() => ({}))
        throw new Error(e.detail || e.error || `HTTP ${resp.status}`)
      }
      const { message: msg } = await import('antd')
      msg.success('净值拉取任务已提交，正在后台执行')
      await waitFor(5000, controller.signal)
      setLoading(true)
      const { data: d } = await request.get<{ items: NavPoint[] }>(`/fund/${code}/nav`, {
        params: { limit: 800 },
        signal: controller.signal,
      })
      if (!controller.signal.aborted) setData(d.items ?? [])
    } catch (e: any) {
      const { message: msg } = await import('antd')
      if (!controller.signal.aborted) msg.error(`拉取失败: ${e?.message || e}`)
    } finally {
      navSyncInFlight.current = false
      if (navAbortRef.current === controller) {
        navAbortRef.current = null
        setNavLoading(false)
      }
    }
  }, [code])

  useEffect(() => {
    const controller = new AbortController()
    navAbortRef.current?.abort()
    navAbortRef.current = controller
    if (!enabled) {
      setLoading(false)
      return () => {
        controller.abort()
        if (navAbortRef.current === controller) navAbortRef.current = null
      }
    }
    setLoading(true)
    setActive(null)
    request
      .get<{ items: NavPoint[] }>(`/fund/${code}/nav`, {
        params: { limit: 800 },
        signal: controller.signal,
      })
      .then(({ data: d }) => {
        if (!controller.signal.aborted) setData(d.items ?? [])
      })
      .catch(() => {
        if (!controller.signal.aborted) setData([])
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => {
      controller.abort()
      if (navAbortRef.current === controller) navAbortRef.current = null
    }
  }, [code, enabled])

  const sliced = useMemo(() => {
    const series = data.filter((p) => Number.isFinite(p.nav))
    if (!range || series.length === 0) return series
    const end = series[series.length - 1].date
    const cutoff = range === -1
      ? dayjs(end).startOf('year')
      : dayjs(end).subtract(range, 'month')
    const win = series.filter((p) => !dayjs(p.date).isBefore(cutoff))
    return win.length >= 2 ? win : series
  }, [data, range])

  const first = sliced[0]?.nav ?? 0
  const last = sliced[sliced.length - 1]?.nav ?? 0
  const lineColor = last >= first ? UP : DOWN
  const shown = active ?? sliced[sliced.length - 1] ?? null
  const shownPct = shown && first ? ((shown.nav - first) / first) * 100 : 0
  const fmtTick = (d: string) => `${d.slice(2, 4)}/${d.slice(5, 7)}`
  const tickGap = Math.max(1, Math.floor(sliced.length / 6))
  const axisTick = { fontSize: 11, fill: token.colorTextTertiary }
  const gradId = `navFill-${code}`

  if (loading) return <div style={{ textAlign: 'center', padding: 40 }}><Spin tip="加载净值中…" /></div>
  if (sliced.length < 2)
    return (
      <Empty
        description={
          <div>
            <div style={{ marginBottom: 8, color: 'var(--text-soft, #999)' }}>暂无净值数据</div>
            <Button size="small" loading={navLoading} onClick={triggerNavSync}>拉取净值</Button>
          </div>
        }
        style={{ padding: 24 }}
      />
    )

  return (
    <div style={{ marginBottom: 16 }}>
      <div style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', gap: 12, marginBottom: 4 }}>
        <div style={{ display: 'flex', gap: 20 }}>
          <Statistic
            title={shown ? `净值 · ${shown.date}` : '净值'}
            value={shown ? shown.nav : 0}
            precision={4}
            valueStyle={{ fontSize: 18 }}
          />
          <Statistic
            title="较区间首日"
            value={shownPct}
            precision={2}
            suffix="%"
            prefix={shownPct >= 0 ? '+' : ''}
            valueStyle={{ fontSize: 18, color: shownPct >= 0 ? UP : DOWN }}
          />
        </div>
        <Segmented size="small" options={NAV_RANGES} value={range} onChange={(v) => setRange(v as number)} />
      </div>
      <ResponsiveContainer width="100%" height={200}>
        <AreaChart
          data={sliced}
          margin={{ top: 8, right: 12, left: 0, bottom: 0 }}
          onMouseMove={(s: any) => {
            const p = s?.activePayload?.[0]?.payload as NavPoint | undefined
            if (p) setActive(p)
          }}
          onMouseLeave={() => setActive(null)}
        >
          <defs>
            <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={lineColor} stopOpacity={0.22} />
              <stop offset="100%" stopColor={lineColor} stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke={token.colorBorderSecondary} />
          <XAxis dataKey="date" tickFormatter={fmtTick} interval={tickGap} tick={axisTick} minTickGap={16} />
          <YAxis domain={['auto', 'auto']} tickFormatter={(v) => v.toFixed(2)} width={48} tick={axisTick} />
          <Tooltip
            cursor={{ stroke: token.colorTextTertiary, strokeDasharray: '3 3' }}
            content={({ active: a, payload }) => {
              if (!a || !payload?.length) return null
              const p = payload[0].payload as NavPoint
              const pp = first ? ((p.nav - first) / first) * 100 : 0
              return (
                <div style={{
                  background: token.colorBgElevated,
                  border: `1px solid ${token.colorBorderSecondary}`,
                  borderRadius: token.borderRadius,
                  padding: '6px 10px', fontSize: 12,
                }}>
                  <div style={{ color: token.colorTextSecondary }}>{p.date}</div>
                  <div style={{ color: token.colorText }}>净值 {p.nav.toFixed(4)}</div>
                  <div style={{ color: pp >= 0 ? UP : DOWN }}>
                    较首日 {pp >= 0 ? '+' : ''}{pp.toFixed(2)}%
                  </div>
                </div>
              )
            }}
          />
          {active && (
            <ReferenceLine y={active.nav} stroke={token.colorTextTertiary} strokeDasharray="3 3" ifOverflow="extendDomain" />
          )}
          <Area type="monotone" dataKey="nav" stroke={lineColor} strokeWidth={1.6} fill={`url(#${gradId})`} isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

export default function FundDetailModal({ code, open, onClose }: Props) {
  const [data, setData] = useState<DetailData | null>(null)
  const [loading, setLoading] = useState(false)
  // 持仓按季度独立加载，可切换历史报告期对比分析
  const [quarters, setQuarters] = useState<string[]>([])
  const [quarter, setQuarter] = useState<string | undefined>()
  const [holdings, setHoldings] = useState<HoldingItem[]>([])
  const [hLoading, setHLoading] = useState(false)
  const holdingsSyncInFlight = useRef(false)
  const aiInFlight = useRef(false)
  const detailAbortRef = useRef<AbortController | null>(null)
  const holdingsAbortRef = useRef<AbortController | null>(null)
  const aiAbortRef = useRef<AbortController | null>(null)

  const loadHoldings = useCallback((c: string, q?: string) => {
    holdingsAbortRef.current?.abort()
    const controller = new AbortController()
    holdingsAbortRef.current = controller
    setHLoading(true)
    request
      .get<{ quarters: string[]; quarter: string | null; holdings: HoldingItem[] }>(
        `/fund/${c}/holdings`, { params: q ? { quarter: q } : {}, signal: controller.signal },
      )
      .then((resp) => {
        if (controller.signal.aborted) return
        setQuarters(resp.data.quarters ?? [])
        setQuarter(resp.data.quarter ?? undefined)
        setHoldings(resp.data.holdings ?? [])
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setQuarters([])
          setQuarter(undefined)
          setHoldings([])
        }
      })
      .finally(() => {
        if (holdingsAbortRef.current === controller) {
          holdingsAbortRef.current = null
          setHLoading(false)
        }
      })
  }, [])
  const triggerHoldingsSync = useCallback(async () => {
    if (!code || holdingsSyncInFlight.current) return
    holdingsSyncInFlight.current = true
    setHLoading(true)
    holdingsAbortRef.current?.abort()
    const controller = new AbortController()
    holdingsAbortRef.current = controller
    try {
      const resp = await rawFetch(`${APP_BASE}/api/fund_holdings/sync`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ codes: [code] }),
        signal: controller.signal,
      })
      if (!resp.ok) {
        const e = await resp.json().catch(() => ({}))
        throw new Error(e.detail || `HTTP ${resp.status}`)
      }
      const { message: msg } = await import('antd')
      msg.success('持仓拉取任务已提交，正在后台执行')
      // 等待 5s 让 worker 完成，再刷新
      await waitFor(5000, controller.signal)
      loadHoldings(code, quarter)
    } catch (e: any) {
      const { message: msg } = await import('antd')
      if (!controller.signal.aborted) {
        msg.error(`拉取失败: ${e?.message || e}`)
        if (holdingsAbortRef.current === controller) setHLoading(false)
      }
    } finally {
      holdingsSyncInFlight.current = false
      if (holdingsAbortRef.current === controller) holdingsAbortRef.current = null
    }
  }, [code, loadHoldings, quarter])


  useEffect(() => {
    const controller = new AbortController()
    detailAbortRef.current?.abort()
    detailAbortRef.current = controller
    if (!open || !code) {
      setData(null); setQuarters([]); setQuarter(undefined); setHoldings([])
      setLoading(false)
      return () => {
        controller.abort()
        if (detailAbortRef.current === controller) detailAbortRef.current = null
        holdingsAbortRef.current?.abort()
        aiAbortRef.current?.abort()
      }
    }
    setLoading(true)
    request
      .get(`/fund/${code}`, { signal: controller.signal })
      .then((resp) => {
        if (!controller.signal.aborted) setData(resp.data)
      })
      .catch(() => {
        if (!controller.signal.aborted) setData(null)
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    loadHoldings(code)
    return () => {
      controller.abort()
      if (detailAbortRef.current === controller) detailAbortRef.current = null
      holdingsAbortRef.current?.abort()
      aiAbortRef.current?.abort()
    }
  }, [open, code, loadHoldings])
  const [aiLoading, setAiLoading] = useState(false)

  const runAiAnalysis = useCallback(async () => {
    if (!code || aiInFlight.current) return
    aiInFlight.current = true
    setAiLoading(true)
    aiAbortRef.current?.abort()
    const controller = new AbortController()
    aiAbortRef.current = controller
    try {
      const resp = await rawFetch(`${APP_BASE}/api/fund/${code}/ai-analyze`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: controller.signal,
      })
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}))
        throw new Error(err.error || `HTTP ${resp.status}`)
      }
      // 消费 SSE 流，等待 done 事件
      const reader = resp.body?.getReader()
      if (!reader) throw new Error('无法读取响应流')
      const decoder = new TextDecoder()
      let buffer = ''
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        if (controller.signal.aborted) return
        buffer += decoder.decode(value, { stream: true })
        // 检查是否收到了 done 事件（SSE 格式: data: {"type":"done",...}）
        if (buffer.includes('"type":"done"')) break
      }
      reader.releaseLock()
      // 重新拉取基金详情获取 ai 字段
      const { data: newData } = await request.get(`/fund/${code}`, { signal: controller.signal })
      if (!controller.signal.aborted) setData(newData as DetailData)
    } catch (e: any) {
      if (!controller.signal.aborted) {
        import('antd').then(({ message: msg }) => msg.error(`AI 分析失败: ${e?.message || e}`))
      }
    } finally {
      aiInFlight.current = false
      if (aiAbortRef.current === controller) {
        aiAbortRef.current = null
        setAiLoading(false)
      }
    }
  }, [code])


  const totalRatio = holdings.reduce((s, h) => s + (h.hold_ratio || 0), 0)
  // 申万一级分组键：A股用申万一级；港股无申万→归到「港股」一组。便于人工看行业分散度。
  const l1Group = (h: HoldingItem) => h.sw_l1 || (h.em_industry ? '港股' : '')
  const swL1Filters = Array.from(new Set(holdings.map(l1Group)))
    .filter((v) => v)
    .sort()
    .map((v) => ({ text: v, value: v }))

  return (
    <Modal open={open} onCancel={onClose} footer={null} width={760} title={`基金详情 · ${code ?? ''}`}>
      <Spin spinning={loading}>
        <Tabs
          items={[
            {
              key: 'basic',
              label: '基础信息',
              children: (
                <>
                  {code && <NavChart code={code} enabled={open} />}
                  <Descriptions size="small" column={2} bordered>
                    {BASIC_FIELDS.map(([k, label]) => (
                      <Descriptions.Item key={k} label={label}>
                        {fmt(data?.[k])}
                      </Descriptions.Item>
                    ))}
                  </Descriptions>
                </>
              ),
            },
            {
              key: 'perf',
              label: '业绩与风险',
              children: (
                <Descriptions size="small" column={2} bordered>
                  {PERF_FIELDS.map(([k, label]) => (
                    <Descriptions.Item key={k} label={label}>
                      {fmt(data?.[k])}
                    </Descriptions.Item>
                  ))}
                </Descriptions>
              ),
            },
            {
              key: 'ai',
              label: 'AI 定性分析',
              children: renderAiPanel(data?.ai as FundAi | null | undefined, runAiAnalysis, aiLoading),
            },
            {
              key: 'holdings',
              label: `股票持仓(${holdings.length})`,
              children: (
                <Spin spinning={hLoading}>
                  <Space align="center" style={{ marginBottom: 12 }} wrap>
                    <Typography.Text type="secondary">报告期</Typography.Text>
                    <Select
                      size="small"
                      style={{ width: 130 }}
                      value={quarter}
                      onChange={(q) => { setQuarter(q); if (code) loadHoldings(code, q) }}
                      options={quarters.map((q) => ({ label: q, value: q }))}
                      placeholder="无持仓数据"
                      disabled={quarters.length === 0}
                    />
                    {holdings.length > 0 && (
                      <Typography.Text type="secondary">
                        前 {holdings.length} 大重仓 · 合计占净值 {totalRatio.toFixed(2)}%
                      </Typography.Text>
                    )}
                  </Space>
                  {holdings.length === 0 ? (
                    <Empty description={<div><div style={{ marginBottom: 8, color: 'var(--text-soft, #999)' }}>该报告期暂无股票持仓数据</div><Button size="small" loading={hLoading} onClick={triggerHoldingsSync}>拉取持仓</Button></div>} />
                  ) : (
                    <Table<HoldingItem>
                      size="small"
                      rowKey={(r) => `${r.holding_type}-${r.asset_code}-${r.quarter}`}
                      dataSource={holdings}
                      pagination={false}
                      columns={[
                        { title: '排名', width: 48, align: 'center', render: (_, __, i) => i + 1 },
                        { title: '代码', dataIndex: 'asset_code', width: 84 },
                        { title: '名称', dataIndex: 'asset_name' },
                        {
                          title: '申万一级', dataIndex: 'sw_l1', width: 96,
                          filters: swL1Filters,
                          onFilter: (val, r) => l1Group(r) === val,
                          render: (v: string, r) => (v
                            ? <Tag>{v}</Tag>
                            : r.em_industry
                              ? <Tag color="gold">港股</Tag>
                              : <span style={{ color: '#bfbfbf' }}>-</span>),
                        },
                        {
                          title: '申万三级 / 东财行业', dataIndex: 'sw_l3', width: 168,
                          render: (v: string, r) => (v
                            ? <Typography.Text style={{ fontSize: 12 }}>
                                {r.sw_l2 ? <span style={{ color: '#8c8c8c' }}>{r.sw_l2} / </span> : null}{v}
                              </Typography.Text>
                            : r.em_industry
                              ? <Typography.Text style={{ fontSize: 12, color: '#d48806' }}>{r.em_industry}</Typography.Text>
                              : <span style={{ color: '#bfbfbf' }}>未映射</span>),
                        },
                        { title: '占净值%', dataIndex: 'hold_ratio', width: 84, align: 'right', render: fmt },
                      ]}
                    />
                  )}
                </Spin>
              ),
            },
          ]}
        />
      </Spin>
    </Modal>
  )
}
