import React from 'react'
import ReactDOM from 'react-dom/client'
import { createMemoryRouter } from 'react-router-dom'
import { APP_BASE, configureAppBase } from './config'
import { setUnauthorizedHandler } from './api/request'
import { createRoutes } from './routes'
import EmbeddedApp, { DEFAULT_DETAIL_CHANNEL } from './EmbeddedApp'
import './index.css'

export interface IfundEmbedOptions {
  basePath?: string
  initialPath?: string
  router?: 'memory'
  embedded?: boolean
  detailChannel?: string
}

export function mountIfundApp(
  container: HTMLElement,
  options: IfundEmbedOptions = {},
) {
  const previousBase = APP_BASE
  const basePath = options.basePath ?? '/ifund'
  const initialPath = options.initialPath ?? `${basePath}/`
  const embedded = options.embedded ?? true
  const detailChannel = options.detailChannel ?? DEFAULT_DETAIL_CHANNEL

  configureAppBase(basePath)

  const router = createMemoryRouter(createRoutes({ embedded }), {
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
      <EmbeddedApp router={router} detailChannel={detailChannel} />
    </React.StrictMode>,
  )

  return {
    unmount() {
      setUnauthorizedHandler(null)
      root.unmount()
      container.innerHTML = ''
      configureAppBase(previousBase)
      delete (window as unknown as Record<string, unknown>)[`__ifundPending_${detailChannel}`]
    },
  }
}
