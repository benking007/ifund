import React from 'react'
import ReactDOM from 'react-dom/client'
import { createMemoryRouter } from 'react-router-dom'
import { APP_BASE, configureAppBase } from './config'
import { setUnauthorizedHandler } from './api/request'
import { routes } from './routes'
import EmbeddedApp from './EmbeddedApp'
import './index.css'

export interface IfundEmbedOptions {
  basePath?: string
  initialPath?: string
  router?: 'memory'
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
