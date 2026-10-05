import { createContext, useContext, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import {
  IconGrid,
  IconWallet,
  IconBarChart,
  IconClock,
  IconActivity,
  IconZap,
  IconPlug,
  IconSettings,
  IconSun,
  IconMoon,
  IconSignOut,
  IconUser,
} from './Icons'
import { BrandMark } from './Logo'
import { VERSION_LABEL, VERSION_TITLE } from '../version'

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

export const MAIN_PAGES = [
  'dashboard',
  'wallets',
  'assets',
  'events',
  'connections',
  'history',
  'schedules',
  'status',
  'account',
  // AUD-436: the assistant is a canned-response prototype, not a working
  // feature. It keeps a route so the intended interface can still be shown
  // on purpose (`#/assistant-prototype`), but it is deliberately absent from
  // the nav and from every in-app link — nobody reaches it by browsing.
  'assistant-prototype',
] as const

export type MainPage = (typeof MAIN_PAGES)[number]

// ---- Nav config ----

const NAV_ITEMS: { page: MainPage; label: string; Icon: React.FC<React.SVGProps<SVGSVGElement>> }[] = [
  { page: 'dashboard', label: 'Dashboard', Icon: IconGrid },
  { page: 'wallets', label: 'Wallets', Icon: IconWallet },
  // AUD-408: Assets is a maintenance registry (exclusions, decimals conflicts,
  // manual contracts), not a portfolio view. It stays routable at #/assets and
  // is reached from the Dashboard links, but it no longer takes a nav slot.
  { page: 'events', label: 'Events', Icon: IconZap },
  { page: 'history', label: 'History', Icon: IconBarChart },
  { page: 'schedules', label: 'Schedules', Icon: IconClock },
  { page: 'status', label: 'Status', Icon: IconActivity },
  { page: 'connections', label: 'Connections', Icon: IconPlug },
  { page: 'account', label: 'Account & Data', Icon: IconSettings },
  // AUD-436: no Assistant entry — see MAIN_PAGES.
]

const PAGE_TITLES: Record<MainPage, string> = {
  dashboard: 'Overview',
  wallets: 'Wallets',
  assets: 'Assets',
  events: 'Events',
  connections: 'Connections',
  history: 'History',
  schedules: 'Schedules',
  status: 'Status',
  account: 'Account & Data',
  'assistant-prototype': 'Assistant (prototype)',
}

// ---- User menu ----

interface UserMenuProps {
  onOpenAccount: () => void
  onSignOut: () => void
}

function UserMenu({ onOpenAccount, onSignOut }: UserMenuProps) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return

    function handlePointerDown(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handlePointerDown)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('mousedown', handlePointerDown)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [open])

  return (
    <div className="user-menu" ref={rootRef}>
      <button
        type="button"
        className="avatar-button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Account menu"
      >
        <IconUser className="avatar-button-icon" />
      </button>
      {open && (
        <div className="user-menu-dropdown" role="menu">
          <button
            type="button"
            className="user-menu-item"
            role="menuitem"
            onClick={() => {
              setOpen(false)
              onOpenAccount()
            }}
          >
            <IconSettings className="nav-item-icon" />
            <span>Account &amp; Data</span>
          </button>
          <button
            type="button"
            className="user-menu-item"
            role="menuitem"
            onClick={() => {
              setOpen(false)
              onSignOut()
            }}
          >
            <IconSignOut className="nav-item-icon" />
            <span>Sign out</span>
          </button>
        </div>
      )}
    </div>
  )
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
          <div className="sidebar-brand">
            <BrandMark label={null} />
            <span className="sidebar-brand-name">audr</span>
          </div>

          <div className="space-card">
            <span className="space-card-name">Personal portfolio</span>
            <span className="space-card-type">Local space</span>
          </div>

          <span className="nav-section-label">Navigation</span>

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
            <span className="sidebar-status">
              <span className="status-dot status-dot-ok" />
              Self-hosted · your data stays yours
            </span>
            {/* AUD-407: bottom-left build identity. Baked into the bundle, so
                it renders even when the API is down. */}
            <span className="sidebar-version" title={VERSION_TITLE} data-testid="app-version">
              {VERSION_LABEL}
            </span>
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
              <UserMenu onOpenAccount={() => setPage('account')} onSignOut={onSignOut} />
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
