import { lazy, Suspense, useState } from 'react'
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { Button, Layout, Menu, theme as antdTheme } from 'antd'
import {
  ApartmentOutlined,
  CalendarOutlined,
  DatabaseOutlined,
  ExperimentOutlined,
  FilterOutlined,
  FundOutlined,
  KeyOutlined,
  LogoutOutlined,
  TeamOutlined,
  WalletOutlined,
} from '@ant-design/icons'
import { AUTH_TOKEN_KEY } from '../config'
import { useDashboardTheme } from '../useDashboardTheme'
import Loading from '../components/Loading'

const FundPage = lazy(() => import('./fund/FundPage'))
const WorkbenchPage = lazy(() => import('./workbench/WorkbenchPage'))
const HoldingsPage = lazy(() => import('./reconcile/HoldingsPage'))
const PerpetualPage = lazy(() => import('./perpetual/PerpetualPage'))
const ManagerPage = lazy(() => import('./manager/ManagerPage'))
const IndustryPage = lazy(() => import('./IndustryPage'))
const TokensPage = lazy(() => import('./TokensPage'))
const TradeCalendar = lazy(() => import('./TradeCalendar'))

const { Header, Sider, Content } = Layout

export default function Dashboard() {
  const navigate = useNavigate()
  const location = useLocation()
  const [collapsed, setCollapsed] = useState(false)
  const { isDark } = useDashboardTheme()
  const { token } = antdTheme.useToken()

  const selected = location.pathname === '/' ? 'fund' : location.pathname.slice(1)

  const logout = () => {
    localStorage.removeItem(AUTH_TOKEN_KEY)
    navigate('/login')
  }

  const go = (key: string) => {
    navigate(key === 'fund' ? '/' : `/${key}`)
  }

  return (
    // 固定视口高度：仅内容区滚动，Header/侧边栏不随滚动移动
    <Layout style={{ height: '100vh' }}>
      <Header
        className="flex items-center justify-between"
        style={{ paddingInline: 16, flexShrink: 0, background: 'var(--ifund-bg-sidebar)' }}
      >
        <span style={{ color: token.colorText, fontSize: 18, fontWeight: 600 }}>iFund</span>
        <Button
          icon={<LogoutOutlined />}
          onClick={logout}
          type="text"
          size="small"
          style={{ color: token.colorText }}
        >
          退出
        </Button>
      </Header>
      <Layout>
        <Sider
          width={160}
          theme={isDark ? 'dark' : 'light'}
          collapsible
          collapsed={collapsed}
          onCollapse={setCollapsed}
          style={{ background: 'var(--ifund-bg-sidebar)' }}
        >
          <Menu
            mode="inline"
            theme={isDark ? 'dark' : 'light'}
            selectedKeys={[selected]}
            defaultOpenKeys={['auxiliary']}
            onClick={(e) => go(e.key)}
            items={[
              {
                type: 'group',
                label: '基金筛选',
                children: [
                  { key: 'fund', icon: <FundOutlined />, label: '基金管理' },
                  { key: 'workbench', icon: <FilterOutlined />, label: '镜像基金' },
                  { key: 'perpetual', icon: <ExperimentOutlined />, label: '永续组合' },
                ],
              },
              { key: 'holdings', icon: <WalletOutlined />, label: '实盘' },
              {
                key: 'auxiliary',
                icon: <DatabaseOutlined />,
                label: '辅助数据',
                children: [
                  { key: 'trade_calendar', icon: <CalendarOutlined />, label: '交易日历' },
                  { key: 'industry', icon: <ApartmentOutlined />, label: '行业映射' },
                  { key: 'manager', icon: <TeamOutlined />, label: '基金经理' },
                ],
              },
              { key: 'tokens', icon: <KeyOutlined />, label: '访问令牌' },
            ]}
          />
        </Sider>
        <Content style={{ padding: 16, overflow: 'auto' }}>
          <Suspense fallback={<Loading />}>
            <Routes>
              <Route path="/" element={<FundPage />} />
              <Route path="/workbench" element={<WorkbenchPage />} />
              <Route path="/perpetual" element={<PerpetualPage />} />
              <Route path="/manager" element={<ManagerPage />} />
              <Route path="/holdings" element={<HoldingsPage />} />
              <Route path="/trade_calendar" element={<TradeCalendar />} />
              <Route path="/industry" element={<IndustryPage />} />
              <Route path="/tokens" element={<TokensPage />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </Suspense>
        </Content>
      </Layout>
    </Layout>
  )
}
