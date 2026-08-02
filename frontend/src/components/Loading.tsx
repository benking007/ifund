import { Spin } from 'antd'

export default function Loading() {
  return (
    <div style={{ minHeight: 240, display: 'grid', placeItems: 'center' }}>
      <Spin size="large" />
    </div>
  )
}
