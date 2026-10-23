import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import './styles/folio.css'
import App from './App'
import ErrorBoundary from './components/ErrorBoundary'

// Freshness here is carried by each page's own refetchInterval (or, for
// ConnectionsPage/ScanStatus, a conditional poll while a job is in flight) —
// not by the client defaults. A 30s staleTime matches that cadence and
// skips a redundant refetch on every remount; refetchOnWindowFocus is off
// because this is a single-owner, locally-hosted app, not a multi-tab
// collaborative one — tab focus is not a freshness signal here.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: false,
    },
  },
})

const root = document.getElementById('root')
if (!root) throw new Error('Root element not found')

createRoot(root).render(
  <StrictMode>
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
)
