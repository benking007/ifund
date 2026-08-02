import axios from 'axios'
import { APP_BASE, AUTH_TOKEN_KEY } from '../config'

// 统一 axios 实例：baseURL=/api，注入 JWT，401 跳登录
const request = axios.create({
  baseURL: `${APP_BASE}/api`,
  timeout: 30000,
})

request.interceptors.request.use((config) => {
  // APP_BASE can change when the SPA is mounted into the dashboard after this
  // module has already been evaluated.
  config.baseURL = `${APP_BASE}/api`
  const token = localStorage.getItem(AUTH_TOKEN_KEY)
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

let unauthorizedHandler: (() => void) | null = null

export function setUnauthorizedHandler(handler: (() => void) | null) {
  unauthorizedHandler = handler
}

request.interceptors.response.use(
  (resp) => resp,
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem(AUTH_TOKEN_KEY)
      if (unauthorizedHandler) {
        unauthorizedHandler()
      } else {
        const loginPath = `${APP_BASE}/login`
        if (window.location.pathname !== loginPath) {
          window.location.href = loginPath
        }
      }
    }
    return Promise.reject(error)
  },
)

export default request
