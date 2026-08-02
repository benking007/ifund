// iFund can run directly at "/" or behind the system dashboard at "/ifund".
// Keep mount detection centralized so routing, API calls, and auth redirects agree.
export let APP_BASE = (
  window.location.pathname === '/ifund' || window.location.pathname.startsWith('/ifund/')
) ? '/ifund' : ''

export const AUTH_TOKEN_KEY = 'ifund_token'

export function configureAppBase(base: string) {
  const normalized = base.trim().replace(/\/+$/, '')
  APP_BASE = normalized && normalized !== '/' ? `/${normalized.replace(/^\/+/, '')}` : ''
}
