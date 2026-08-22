import { useEffect, useState } from 'react'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import 'dayjs/locale/zh-cn'
import { createDashboardTheme, useDashboardTheme } from './useDashboardTheme'
import FundDetailModal from './pages/fund/components/FundDetailModal'

export const DEFAULT_DETAIL_CHANNEL = 'ifund:open-detail'

interface EmbeddedAppProps {
  router: ReturnType<typeof createMemoryRouter>
  detailChannel?: string
}

function pendingKey(channel: string): string {
  return `__ifundPending_${channel}`
}

function pendingStore(): Record<string, unknown> {
  return window as unknown as Record<string, unknown>
}

function consumePendingCode(channel: string): string | null {
  const key = pendingKey(channel)
  const store = pendingStore()
  const raw = store[key]
  if (raw == null) return null
  delete store[key]
  const code = String(raw).trim()
  return code || null
}

export default function EmbeddedApp({
  router,
  detailChannel = DEFAULT_DETAIL_CHANNEL,
}: EmbeddedAppProps) {
  const { isDark, themeName } = useDashboardTheme()
  const [detailCode, setDetailCode] = useState<string | null>(null)

  useEffect(() => {
    const applyCode = (code: unknown) => {
      if (typeof code !== 'string') return
      const next = code.trim()
      if (next) setDetailCode(next)
    }
    const handler = (event: Event) => {
      applyCode((event as CustomEvent<{ code?: string }>).detail?.code)
    }
    window.addEventListener(detailChannel, handler)
    applyCode(consumePendingCode(detailChannel))
    return () => {
      window.removeEventListener(detailChannel, handler)
    }
  }, [detailChannel])

  return (
    <div className="ifund-app" data-theme={themeName}>
      <ConfigProvider locale={zhCN} theme={createDashboardTheme(isDark)}>
        <RouterProvider router={router} />
        <FundDetailModal
          code={detailCode}
          open={!!detailCode}
          onClose={() => setDetailCode(null)}
        />
      </ConfigProvider>
    </div>
  )
}
