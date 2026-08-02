import type { ReactNode } from 'react'
import { Navigate } from 'react-router-dom'
import { AUTH_TOKEN_KEY } from './config'

interface RequireAuthProps {
  children: ReactNode
}

export default function RequireAuth({ children }: RequireAuthProps) {
  const token = localStorage.getItem(AUTH_TOKEN_KEY)
  return token ? children : <Navigate to="/login" replace />
}
