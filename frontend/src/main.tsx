import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import 'dayjs/locale/zh-cn'
import App from './App'
import { APP_BASE } from './config'
import { createDashboardTheme, useDashboardTheme } from './useDashboardTheme'
import './index.css'

function StandaloneApp() {
  const { isDark, themeName } = useDashboardTheme()

  return (
    <div className="ifund-app" data-theme={themeName}>
      <ConfigProvider locale={zhCN} theme={createDashboardTheme(isDark)}>
        <BrowserRouter basename={APP_BASE || undefined}>
          <App />
        </BrowserRouter>
      </ConfigProvider>
    </div>
  )
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <StandaloneApp />
  </React.StrictMode>,
)
