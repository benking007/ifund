import { useEffect, useState } from 'react'
import { Table, Tag } from 'antd'
import request from '../../api/request'
import type { TenureSegment } from './types'

export default function TenureHistory({ code }: { code: string }) {
  const [rows, setRows] = useState<TenureSegment[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    request.get(`/fund_manager/tenure/${code}`).then((res) => {
      setRows(res.data)
    }).finally(() => setLoading(false))
  }, [code])

  const columns = [
    { title: '#', dataIndex: 'seq', key: 'seq', width: 40 },
    { title: '起始', dataIndex: 'start_date', key: 'start', width: 110 },
    {
      title: '截止', key: 'end', width: 110,
      render: (_: unknown, r: TenureSegment) =>
        r.end_date ?? <Tag color="green">至今</Tag>,
    },
    { title: '基金经理', dataIndex: 'managers', key: 'managers', width: 140 },
    { title: '任职时长', dataIndex: 'tenure_text', key: 'tenure_text', width: 110 },
    {
      title: '任期回报', dataIndex: 'tenure_return', key: 'tenure_return', width: 90,
      render: (v: number | null) => v != null ? `${v.toFixed(2)}%` : '-',
    },
  ]

  return (
    <Table
      dataSource={rows}
      columns={columns}
      rowKey="seq"
      size="small"
      loading={loading}
      pagination={false}
      onRow={(r) =>
        r.is_current === 1
          ? { style: { background: 'rgba(24, 144, 255, 0.15)' } }
          : {}
      }
    />
  )
}
