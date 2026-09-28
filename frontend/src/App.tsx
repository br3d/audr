import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchSetupStatus, fetchSession, logout, AuthError } from './api/client'
import SetupPage from './pages/SetupPage'
import SignInPage from './pages/SignInPage'
import DashboardPage from './pages/DashboardPage'
import HoldingsPage from './pages/HoldingsPage'
import WalletsPage from './pages/WalletsPage'
import AssetsPage from './pages/AssetsPage'
import ConnectionsPage from './pages/ConnectionsPage'
import HistoryPage from './pages/HistoryPage'
import SchedulesPage from './pages/SchedulesPage'
import StatusPage from './pages/StatusPage'
import AccountDataPage from './pages/AccountDataPage'

type MainPage =
  | 'dashboard'
  | 'holdings'
  | 'wallets'
  | 'assets'
  | 'connections'
  | 'history'
  | 'schedules'
  | 'status'
  | 'account'

export default function App() {
  const queryClient = useQueryClient()
  const [page, setPage] = useState<MainPage>('dashboard')

  const setupQuery = useQuery({
    queryKey: ['setup-status'],
    queryFn: fetchSetupStatus,
    retry: false,
  })

  const sessionQuery = useQuery({
    queryKey: ['session'],
    queryFn: fetchSession,
    enabled: setupQuery.data?.setup_required === false,
    retry: (_, error) => !(error instanceof AuthError),
  })

  function handleSetupComplete() {
    queryClient.setQueryData(['setup-status'], { setup_required: false })
  }

  function handleSignIn() {
    queryClient.resetQueries({ queryKey: ['session'] })
  }

  async function handleSignOut() {
    try {
      await logout()
    } catch {
      // logout() already clears the CSRF token on failure
    }
    queryClient.resetQueries({ queryKey: ['session'] })
  }

  if (setupQuery.isPending) {
    return <div aria-busy="true">Loading…</div>
  }

  if (setupQuery.isError) {
    return <div role="alert">Failed to connect to server. Please reload.</div>
  }

  if (setupQuery.data.setup_required) {
    return <SetupPage onSetupComplete={handleSetupComplete} />
  }

  if (sessionQuery.isPending) {
    return <div aria-busy="true">Loading…</div>
  }

  if (sessionQuery.isError) {
    return <SignInPage onSignIn={handleSignIn} />
  }

  return (
    <div>
      <nav>
        <button
          onClick={() => setPage('dashboard')}
          aria-current={page === 'dashboard' ? 'page' : undefined}
        >
          Dashboard
        </button>
        <button
          onClick={() => setPage('holdings')}
          aria-current={page === 'holdings' ? 'page' : undefined}
        >
          Holdings
        </button>
        <button
          onClick={() => setPage('wallets')}
          aria-current={page === 'wallets' ? 'page' : undefined}
        >
          Wallets
        </button>
        <button
          onClick={() => setPage('assets')}
          aria-current={page === 'assets' ? 'page' : undefined}
        >
          Assets
        </button>
        <button
          onClick={() => setPage('connections')}
          aria-current={page === 'connections' ? 'page' : undefined}
        >
          Connections
        </button>
        <button
          onClick={() => setPage('history')}
          aria-current={page === 'history' ? 'page' : undefined}
        >
          History
        </button>
        <button
          onClick={() => setPage('schedules')}
          aria-current={page === 'schedules' ? 'page' : undefined}
        >
          Schedules
        </button>
        <button
          onClick={() => setPage('status')}
          aria-current={page === 'status' ? 'page' : undefined}
        >
          Status
        </button>
        <button
          onClick={() => setPage('account')}
          aria-current={page === 'account' ? 'page' : undefined}
        >
          Account &amp; Data
        </button>
        <button onClick={handleSignOut}>Sign out</button>
      </nav>
      {page === 'dashboard' && <DashboardPage />}
      {page === 'holdings' && <HoldingsPage />}
      {page === 'wallets' && <WalletsPage />}
      {page === 'assets' && <AssetsPage />}
      {page === 'connections' && <ConnectionsPage />}
      {page === 'history' && <HistoryPage />}
      {page === 'schedules' && <SchedulesPage />}
      {page === 'status' && <StatusPage />}
      {page === 'account' && <AccountDataPage />}
    </div>
  )
}
