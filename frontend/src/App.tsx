import { useState, useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchSetupStatus, fetchSession, logout, AuthError, setUnauthorizedCallback, clearUnauthorizedCallback } from './api/client'
import Layout from './components/Layout'
import type { MainPage } from './components/Layout'
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
import AssistantPage from './pages/AssistantPage'

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

  // Register a global 401 handler so any API call that encounters an expired
  // session (not just the initial session query) redirects to sign-in.
  // Guard: only reset when session is currently 'success'. If the session query
  // itself 401s, its status is 'pending' (or already 'error'), so the guard
  // blocks the callback and prevents an infinite reset/refetch loop.
  useEffect(() => {
    setUnauthorizedCallback(() => {
      if (queryClient.getQueryState(['session'])?.status === 'success') {
        queryClient.resetQueries({ queryKey: ['session'] })
      }
    })
    return () => {
      clearUnauthorizedCallback()
    }
  }, [queryClient])

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
    <Layout page={page} setPage={setPage} onSignOut={handleSignOut}>
      {page === 'dashboard' && <DashboardPage />}
      {page === 'holdings' && <HoldingsPage />}
      {page === 'wallets' && <WalletsPage />}
      {page === 'assets' && <AssetsPage />}
      {page === 'connections' && <ConnectionsPage />}
      {page === 'history' && <HistoryPage />}
      {page === 'schedules' && <SchedulesPage />}
      {page === 'status' && <StatusPage />}
      {page === 'account' && <AccountDataPage />}
      {page === 'assistant' && <AssistantPage />}
    </Layout>
  )
}
