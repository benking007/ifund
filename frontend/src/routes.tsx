import { lazy, Suspense } from 'react'
import type { RouteObject } from 'react-router-dom'
import Loading from './components/Loading'
import RequireAuth from './RequireAuth'

const Dashboard = lazy(() => import('./pages/Dashboard'))
const Login = lazy(() => import('./pages/Login'))

export const routes: RouteObject[] = [
  {
    path: '/login',
    element: (
      <Suspense fallback={<Loading />}>
        <Login />
      </Suspense>
    ),
  },
  {
    path: '/*',
    element: (
      <RequireAuth>
        <Suspense fallback={<Loading />}>
          <Dashboard />
        </Suspense>
      </RequireAuth>
    ),
  },
]
