import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import 'dayjs/locale/zh-cn'
import { createDashboardTheme, useDashboardTheme } from './useDashboardTheme'

interface EmbeddedAppProps {
  router: ReturnType<typeof createMemoryRouter>
}

export default function EmbeddedApp({ router }: EmbeddedAppProps) {
  const { isDark, themeName } = useDashboardTheme()

  return (
    <div className="ifund-app" data-theme={themeName}>
      <ConfigProvider locale={zhCN} theme={createDashboardTheme(isDark)}>
        <RouterProvider router={router} />
      </ConfigProvider>
    </div>
  )
}
