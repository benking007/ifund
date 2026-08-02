import React from 'react'
import ReactDOM from 'react-dom/client'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import 'dayjs/locale/zh-cn'
import { APP_BASE, configureAppBase } from './config'
import { setUnauthorizedHandler } from './api/request'
import { routes } from './routes'
import { createDashboardTheme, useDashboardTheme } from './useDashboardTheme'
import './index.css'

export interface IfundEmbedOptions {
  basePath?: string
  initialPath?: string
  router?: 'memory'
}

function EmbeddedApp({ router }: { router: ReturnType<typeof createMemoryRouter> }) {
  const { isDark, themeName } = useDashboardTheme()

  return (
    <div className="ifund-app" data-theme={themeName}>
      <ConfigProvider locale={zhCN} theme={createDashboardTheme(isDark)}>
        <RouterProvider router={router} />
      </ConfigProvider>
    </div>
  )
}

export function mountIfundApp(
  container: HTMLElement,
  options: IfundEmbedOptions = {},
) {
  const previousBase = APP_BASE
  const basePath = options.basePath ?? '/ifund'
  const initialPath = options.initialPath ?? `${basePath}/`

  configureAppBase(basePath)

  const router = createMemoryRouter(routes, {
    basename: basePath || undefined,
    initialEntries: [initialPath],
  })
  setUnauthorizedHandler(() => {
    void router.navigate('/login')
  })

  container.innerHTML = ''
  const root = ReactDOM.createRoot(container)
  root.render(
    <React.StrictMode>
      <EmbeddedApp router={router} />
    </React.StrictMode>,
  )

  return {
    unmount() {
      setUnauthorizedHandler(null)
      root.unmount()
      container.innerHTML = ''
      configureAppBase(previousBase)
    },
  }
}
