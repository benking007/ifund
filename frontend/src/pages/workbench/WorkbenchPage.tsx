import { useEffect, useState } from 'react'
import { Card, Select, Space } from 'antd'
import request from '../../api/request'
import type { QueryPreset } from '../fund/types'
import MirrorView from '../screen/MirrorView'

export default function WorkbenchPage() {
  const [presets, setPresets] = useState<QueryPreset[]>([])
  const [presetId, setPresetId] = useState<number | null>(null)

  useEffect(() => {
    request
      .get('/fund/presets')
      .then(({ data }) => setPresets(data.items ?? data ?? []))
      .catch(() => undefined)
  }, [])

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Card size="small">
        <Space wrap>
          <span className="text-gray-400">选择预设：</span>
          <Select
            placeholder="请选择基金预设条件"
            style={{ minWidth: 260 }}
            value={presetId ?? undefined}
            onChange={setPresetId}
            options={presets.map((p) => ({ label: p.name, value: p.id }))}
          />
          <span style={{ color: '#999', fontSize: 12 }}>
            镜像基金为永续组合提供候选池
          </span>
        </Space>
      </Card>

      <MirrorView presetId={presetId} presets={presets} />
    </div>
  )
}
