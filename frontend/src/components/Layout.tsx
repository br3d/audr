import { createContext, useContext, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import {
  IconGrid,
  IconLayers,
  IconWallet,
  IconCoins,
  IconBarChart,
  IconClock,
  IconActivity,
  IconPlug,
  IconSettings,
  IconSun,
  IconMoon,
  IconSignOut,
  IconChat,
} from './Icons'

// ---- Theme context ----

type Theme = 'dark' | 'light'

const ThemeCtx = createContext<{ theme: Theme; toggle: () => void }>({
  theme: 'dark',
  toggle: () => {},
})

export function useTheme() {
  return useContext(ThemeCtx)
}

// ---- Page types ----

export type MainPage =
  | 'dashboard'
  | 'holdings'
  | 'wallets'
  | 'assets'
  | 'connections'
  | 'history'
  | 'schedules'
  | 'status'
  | 'account'
  | 'assistant'

// ---- Nav config ----

const NAV_ITEMS: { page: MainPage; label: string; Icon: React.FC<React.SVGProps<SVGSVGElement>> }[] = [
  { page: 'dashboard', label: 'Dashboard', Icon: IconGrid },
  { page: 'holdings', label: 'Holdings', Icon: IconLayers },
  { page: 'wallets', label: 'Wallets', Icon: IconWallet },
  { page: 'assets', label: 'Assets', Icon: IconCoins },
  { page: 'history', label: 'History', Icon: IconBarChart },
  { page: 'schedules', label: 'Schedules', Icon: IconClock },
  { page: 'status', label: 'Status', Icon: IconActivity },
  { page: 'connections', label: 'Connections', Icon: IconPlug },
  { page: 'account', label: 'Account & Data', Icon: IconSettings },
  { page: 'assistant', label: 'Assistant', Icon: IconChat },
]

const PAGE_TITLES: Record<MainPage, string> = {
  dashboard: 'Overview',
  holdings: 'Holdings',
  wallets: 'Wallets',
  assets: 'Assets',
  connections: 'Connections',
  history: 'History',
  schedules: 'Schedules',
  status: 'Status',
  account: 'Account & Data',
  assistant: 'Assistant',
}

// ---- Layout component ----

interface LayoutProps {
  page: MainPage
  setPage: (page: MainPage) => void
  onSignOut: () => void
  children: ReactNode
}

export default function Layout({ page, setPage, onSignOut, children }: LayoutProps) {
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      return (localStorage.getItem('folio-theme') as Theme) ?? 'dark'
    } catch {
      return 'dark'
    }
  })

  useEffect(() => {
    document.documentElement.dataset.theme = theme === 'light' ? 'light' : ''
    try {
      localStorage.setItem('folio-theme', theme)
    } catch {
      // ignore
    }
  }, [theme])

  function toggle() {
    setTheme((t) => (t === 'dark' ? 'light' : 'dark'))
  }

  return (
    <ThemeCtx.Provider value={{ theme, toggle }}>
      <div className="app-shell">
        <aside className="sidebar" role="navigation" aria-label="Main navigation">
          <div className="sidebar-logo">
            <div className="sidebar-logo-mark">A</div>
            <span className="sidebar-logo-name">audr</span>
          </div>

          <nav className="sidebar-nav">
            {NAV_ITEMS.map(({ page: p, label, Icon }) => (
              <button
                key={p}
                type="button"
                className={`nav-item${page === p ? ' active' : ''}`}
                onClick={() => setPage(p)}
                aria-current={page === p ? 'page' : undefined}
              >
                <Icon className="nav-item-icon" />
                <span>{label}</span>
              </button>
            ))}
          </nav>

          <div className="sidebar-footer">
            <button
              type="button"
              className="nav-item"
              onClick={onSignOut}
            >
              <IconSignOut className="nav-item-icon" />
              <span>Sign out</span>
            </button>
          </div>
        </aside>

        <div className="main-panel">
          <header className="topbar">
            <h1 className="topbar-title">{PAGE_TITLES[page]}</h1>
            <div className="topbar-actions">
              <button
                type="button"
                className="theme-toggle"
                onClick={toggle}
                aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
                title={theme === 'dark' ? 'Light theme' : 'Dark theme'}
              >
                {theme === 'dark' ? <IconSun width={17} height={17} /> : <IconMoon width={17} height={17} />}
              </button>
            </div>
          </header>

          <main className="page-content">
            {children}
          </main>
        </div>
      </div>
    </ThemeCtx.Provider>
  )
}
