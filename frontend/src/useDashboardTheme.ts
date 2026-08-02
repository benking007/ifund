import { useEffect, useState } from 'react'
import { theme } from 'antd'
import type { ThemeConfig } from 'antd'

const DASHBOARD_THEME_KEY = 'dashboard-theme'

type DashboardThemeName = 'light' | 'dark'

interface DashboardPalette {
  bgApp: string
  bgCard: string
  bgSidebar: string
  text: string
  textSoft: string
  accent: string
  accentHover: string
  border: string
  rise: string
  fall: string
}

export const dashboardPalettes: Record<DashboardThemeName, DashboardPalette> = {
  light: {
    bgApp: '#F5F3EE',
    bgCard: '#FFFFFF',
    bgSidebar: '#F0EEE6',
    text: '#2D2B26',
    textSoft: '#5B584F',
    accent: '#C96442',
    accentHover: '#B24F30',
    border: '#E5E1D8',
    rise: '#C43A3A',
    fall: '#3F8559',
  },
  dark: {
    bgApp: '#262624',
    bgCard: '#2F2D2A',
    bgSidebar: '#1F1E1C',
    text: '#EDE9E0',
    textSoft: '#B8B3A7',
    accent: '#D97757',
    accentHover: '#E18C70',
    border: '#3D3A34',
    rise: '#E25555',
    fall: '#5CA373',
  },
}

function getStoredTheme(): DashboardThemeName | null {
  try {
    const storedTheme = localStorage.getItem(DASHBOARD_THEME_KEY)
    return storedTheme === 'light' || storedTheme === 'dark' ? storedTheme : null
  } catch {
    return null
  }
}

function resolveDashboardTheme(): DashboardThemeName {
  const documentTheme = document.documentElement.getAttribute('data-theme')
  if (documentTheme === 'light' || documentTheme === 'dark') return documentTheme

  const storedTheme = getStoredTheme()
  if (storedTheme) return storedTheme

  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

export function useDashboardTheme() {
  const [themeName, setThemeName] = useState<DashboardThemeName>(resolveDashboardTheme)

  useEffect(() => {
    const colorScheme = window.matchMedia('(prefers-color-scheme: dark)')
    const syncTheme = () => setThemeName(resolveDashboardTheme())
    const syncSystemTheme = () => {
      const documentTheme = document.documentElement.getAttribute('data-theme')
      if (!documentTheme && !getStoredTheme()) syncTheme()
    }

    const observer = new MutationObserver(syncTheme)
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme'],
    })
    window.addEventListener('storage', syncTheme)
    colorScheme.addEventListener('change', syncSystemTheme)

    return () => {
      observer.disconnect()
      window.removeEventListener('storage', syncTheme)
      colorScheme.removeEventListener('change', syncSystemTheme)
    }
  }, [])

  return {
    isDark: themeName === 'dark',
    themeName,
  }
}

export function createDashboardTheme(isDark: boolean): ThemeConfig {
  const palette = dashboardPalettes[isDark ? 'dark' : 'light']

  return {
    algorithm: isDark ? theme.darkAlgorithm : theme.defaultAlgorithm,
    token: {
      colorPrimary: palette.accent,
      colorPrimaryHover: palette.accentHover,
      colorBgBase: palette.bgApp,
      colorBgLayout: palette.bgApp,
      colorBgContainer: palette.bgCard,
      colorBgElevated: palette.bgCard,
      colorText: palette.text,
      colorTextSecondary: palette.textSoft,
      colorTextTertiary: palette.textSoft,
      colorBorder: palette.border,
      colorBorderSecondary: palette.border,
      colorSplit: palette.border,
      colorError: palette.rise,
      colorSuccess: palette.fall,
    },
    components: {
      Layout: {
        bodyBg: palette.bgApp,
        headerBg: palette.bgSidebar,
        siderBg: palette.bgSidebar,
      },
      Menu: {
        itemBg: palette.bgSidebar,
        subMenuItemBg: palette.bgSidebar,
        itemColor: palette.textSoft,
        itemSelectedColor: palette.accent,
        itemSelectedBg: `${palette.accent}1F`,
        darkItemBg: palette.bgSidebar,
        darkSubMenuItemBg: palette.bgSidebar,
        darkItemColor: palette.textSoft,
        darkItemSelectedBg: palette.accent,
        darkItemSelectedColor: palette.bgCard,
      },
    },
  }
}
