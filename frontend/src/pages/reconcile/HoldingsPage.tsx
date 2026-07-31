import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Button, Card, Input, Modal, Popconfirm, Select, Space, Tabs, message,
} from 'antd'
import { DeleteOutlined, EditOutlined, PlusOutlined } from '@ant-design/icons'
import request from '../../api/request'
import type { Portfolio } from './types'
import HoldingsManager from './HoldingsManager'
import HoldingsPenetration from './HoldingsPenetration'
import ReconcileView from './ReconcileView'

export default function HoldingsPage() {
  const [portfolios, setPortfolios] = useState<Portfolio[]>([])
  const [pid, setPid] = useState<number | null>(null)
  const [editOpen, setEditOpen] = useState(false)
  const [editMode, setEditMode] = useState<'create' | 'rename'>('create')
  const [editName, setEditName] = useState('')
  const [reloadSignal, setReloadSignal] = useState(0)

  const current = useMemo(() => portfolios.find((p) => p.id === pid) ?? null, [portfolios, pid])

  const loadPortfolios = useCallback(async (selectId?: number) => {
    try {
      const { data } = await request.get<{ items: Portfolio[] }>('/reconcile/portfolios')
      const items = data.items ?? []
      setPortfolios(items)
      setPid((prev) => {
        if (selectId && items.some((p) => p.id === selectId)) return selectId
        if (prev && items.some((p) => p.id === prev)) return prev
        return items[0]?.id ?? null
      })
    } catch {
      message.error('加载实盘列表失败')
    }
  }, [])

  useEffect(() => { loadPortfolios() }, [loadPortfolios])

  const openCreate = () => {
    setEditMode('create')
    setEditName('')
    setEditOpen(true)
  }
  const openRename = () => {
    if (!current) return
    setEditMode('rename')
    setEditName(current.name)
    setEditOpen(true)
  }
  const submitEdit = async () => {
    const name = editName.trim()
    if (!name) {
      message.warning('请输入实盘名称')
      return
    }
    try {
      if (editMode === 'create') {
        const { data } = await request.post<Portfolio>('/reconcile/portfolios', { name })
        await loadPortfolios(data.id)
        message.success('已新建实盘')
      } else if (current) {
        await request.patch(`/reconcile/portfolios/${current.id}`, { name })
        setPortfolios((prev) => prev.map((p) => (p.id === current.id ? { ...p, name } : p)))
        message.success('已重命名')
      }
      setEditOpen(false)
    } catch {
      message.error('操作失败')
    }
  }

  const removePortfolio = async () => {
    if (!current) return
    try {
      await request.delete(`/reconcile/portfolios/${current.id}`)
      message.success('已删除实盘')
      await loadPortfolios()
    } catch {
      message.error('删除失败')
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Card size="small" title="选择实盘">
        <Space wrap size="middle">
          <Select
            style={{ minWidth: 220 }}
            placeholder="选择实盘"
            value={pid ?? undefined}
            onChange={setPid}
            options={portfolios.map((p) => ({ label: p.name, value: p.id }))}
          />
          <Button icon={<PlusOutlined />} onClick={openCreate}>
            新建
          </Button>
          <Button icon={<EditOutlined />} onClick={openRename} disabled={!current}>
            重命名
          </Button>
          <Popconfirm
            title="删除该实盘？"
            description="该实盘下的持仓将一并删除，不可恢复。"
            onConfirm={removePortfolio}
            disabled={!current || portfolios.length <= 1}
          >
            <Button icon={<DeleteOutlined />} danger disabled={!current || portfolios.length <= 1}>
              删除
            </Button>
          </Popconfirm>
        </Space>
        <div style={{ marginTop: 8 }}>
          <span style={{ color: '#999', fontSize: 12 }}>
            目标权重来自「永续组合」最近一次保存的持仓与权重。录入真实持仓后即可生成调仓建议。
          </span>
        </div>
      </Card>

      <Tabs
        defaultActiveKey="holdings"
        items={[
          {
            key: 'holdings',
            label: '实际持仓管理',
            children: <HoldingsManager portfolioId={pid} reloadSignal={reloadSignal} />,
          },
          {
            key: 'penetration',
            label: '底层穿透',
            children: <HoldingsPenetration portfolioId={pid} reloadSignal={reloadSignal} />,
          },
          {
            key: 'reconcile',
            label: '调仓建议',
            children: (
              <ReconcileView
                portfolioId={pid}
                onSavedTxns={() => setReloadSignal((s) => s + 1)}
              />
            ),
          },
        ]}
      />

      <Modal
        open={editOpen}
        title={editMode === 'create' ? '新建实盘' : '重命名实盘'}
        onOk={submitEdit}
        onCancel={() => setEditOpen(false)}
        okText="确定"
        cancelText="取消"
        destroyOnClose
      >
        <Input
          autoFocus
          placeholder="实盘名称，如：我的实盘 / 老王的钱"
          value={editName}
          onChange={(e) => setEditName(e.target.value)}
          onPressEnter={submitEdit}
        />
      </Modal>
    </div>
  )
}
