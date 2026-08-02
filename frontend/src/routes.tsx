import type { RouteObject } from 'react-router-dom'
import { Navigate } from 'react-router-dom'
import { AUTH_TOKEN_KEY } from './config'
import Dashboard from './pages/Dashboard'
import Login from './pages/Login'

function RequireAuth({ children }: { children: JSX.Element }) {
  const token = localStorage.getItem(AUTH_TOKEN_KEY)
  return token ? children : <Navigate to="/login" replace />
}

export const routes: RouteObject[] = [
  {
    path: '/login',
    element: <Login />,
  },
  {
    path: '/*',
    element: (
      <RequireAuth>
        <Dashboard />
      </RequireAuth>
    ),
  },
]
