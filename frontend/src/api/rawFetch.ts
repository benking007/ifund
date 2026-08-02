import { APP_BASE, AUTH_TOKEN_KEY } from '../config'

function redirectToLogin() {
  localStorage.removeItem(AUTH_TOKEN_KEY)
  const loginPath = `${APP_BASE}/login`
  if (window.location.pathname !== loginPath) {
    window.location.href = loginPath
  }
}

/** Native fetch with the same auth header and 401 behavior as the Axios client. */
export default async function rawFetch(
  input: RequestInfo | URL,
  init: RequestInit = {},
): Promise<Response> {
  const headers = new Headers(init.headers)
  if (!headers.has('Authorization')) {
    const token = localStorage.getItem(AUTH_TOKEN_KEY)
    if (token) headers.set('Authorization', `Bearer ${token}`)
  }

  const response = await fetch(input, { ...init, headers })
  if (response.status === 401) redirectToLogin()
  return response
}
