import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// Mock pages so tests focus on routing/auth logic, not page internals
vi.mock('../pages/SetupPage', () => ({
  default: ({ onSetupComplete }: { onSetupComplete: () => void }) =>
    React.createElement(
      'div',
      { 'data-testid': 'setup-page' },
      React.createElement('button', { onClick: onSetupComplete }, 'complete-setup'),
    ),
}))

vi.mock('../pages/SignInPage', () => ({
  default: ({ onSignIn }: { onSignIn: () => void }) =>
    React.createElement(
      'div',
      { 'data-testid': 'sign-in-page' },
      React.createElement('button', { onClick: onSignIn }, 'sign-in'),
    ),
}))

vi.mock('../pages/DashboardPage', () => ({
  default: () => React.createElement('div', { 'data-testid': 'dashboard-page' }),
}))
vi.mock('../pages/WalletsPage', () => ({
  default: () => React.createElement('div', { 'data-testid': 'wallets-page' }),
}))
vi.mock('../pages/AssetsPage', () => ({
  default: () => React.createElement('div', { 'data-testid': 'assets-page' }),
}))
vi.mock('../pages/ConnectionsPage', () => ({
  default: () => React.createElement('div', { 'data-testid': 'connections-page' }),
}))

vi.mock('../api/client', () => {
  class AuthError extends Error {
    status = 401
    body = undefined
    constructor() {
      super('Unauthorized')
      this.name = 'AuthError'
    }
  }
  return {
    fetchSetupStatus: vi.fn(),
    fetchSession: vi.fn(),
    logout: vi.fn(),
    setCSRFToken: vi.fn(),
    clearCSRFToken: vi.fn(),
    getCSRFToken: vi.fn(),
    setUnauthorizedCallback: vi.fn(),
    clearUnauthorizedCallback: vi.fn(),
    AuthError,
  }
})

import App from '../App'
import { fetchSetupStatus, fetchSession, logout, AuthError, setUnauthorizedCallback } from '../api/client'

const mockSetupStatus = vi.mocked(fetchSetupStatus)
const mockSession = vi.mocked(fetchSession)
const mockLogout = vi.mocked(logout)
const mockSetUnauthorizedCallback = vi.mocked(setUnauthorizedCallback)

const SESSION_OK = { authenticated: true as const, expires_at: '2099-01-01T00:00:00Z', csrf_token: 'tok' }

// QueryClient with staleTime: Infinity so pre-populated cache never triggers background refetches
function makeQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  })
}

describe('App routing and auth guard', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    // The active tab now lives in location.hash, which is shared jsdom state —
    // reset it so one test's navigation does not leak into the next.
    window.history.replaceState(null, '', '/')
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    // Safe defaults so tests that don't care about mocks don't break
    mockSetupStatus.mockResolvedValue({ setup_required: false })
    mockSession.mockResolvedValue(SESSION_OK)
    mockLogout.mockResolvedValue(undefined)
  })

  afterEach(async () => {
    await act(async () => {
      root.unmount()
    })
    document.body.removeChild(container)
    vi.clearAllMocks()
  })

  // Pre-populate cache so queries return data synchronously without async fetching
  function mountWithCache(
    setupData: { setup_required: boolean },
    sessionData?: typeof SESSION_OK,
  ) {
    const qc = makeQueryClient()
    qc.setQueryData(['setup-status'], setupData)
    if (sessionData) qc.setQueryData(['session'], sessionData)
    act(() => {
      root.render(
        React.createElement(QueryClientProvider, { client: qc }, React.createElement(App)),
      )
    })
    return { qc }
  }

  it('shows loading indicator while setup status is pending', async () => {
    mockSetupStatus.mockReturnValue(new Promise(() => {})) // never resolves
    const qc = makeQueryClient()
    await act(async () => {
      root.render(
        React.createElement(QueryClientProvider, { client: qc }, React.createElement(App)),
      )
    })
    expect(container.querySelector('[aria-busy="true"]')).toBeTruthy()
  })

  it('shows SetupPage when setup_required is true', () => {
    mountWithCache({ setup_required: true })
    expect(container.querySelector('[data-testid="setup-page"]')).toBeTruthy()
    expect(container.querySelector('[data-testid="sign-in-page"]')).toBeNull()
  })

  it('shows SignInPage when setup is done and session returns 401', async () => {
    mockSetupStatus.mockResolvedValue({ setup_required: false })
    mockSession.mockRejectedValue(new AuthError())
    const qc = makeQueryClient()
    // Pre-populate setup-status so setup query resolves synchronously; session query will fetch+fail
    qc.setQueryData(['setup-status'], { setup_required: false })
    await act(async () => {
      root.render(
        React.createElement(QueryClientProvider, { client: qc }, React.createElement(App)),
      )
    })
    await vi.waitFor(
      () => expect(container.querySelector('[data-testid="sign-in-page"]')).toBeTruthy(),
      { timeout: 1000 },
    )
    expect(container.querySelector('[data-testid="setup-page"]')).toBeNull()
  })

  it('shows tab navigation and Dashboard page when authenticated', () => {
    mountWithCache({ setup_required: false }, SESSION_OK)
    expect(container.querySelector('[data-testid="dashboard-page"]')).toBeTruthy()
    expect(container.textContent).toContain('Dashboard')
    expect(container.textContent).not.toContain('Holdings')
    expect(container.textContent).toContain('Wallets')
    expect(container.textContent).toContain('Assets')
    expect(container.textContent).toContain('Connections')
    // "Sign out" lives in the user menu now; it opens from the avatar button in the topbar.
    expect(container.querySelector('[aria-label="Account menu"]')).toBeTruthy()
  })

  it('switches to Wallets page on tab click', async () => {
    mountWithCache({ setup_required: false }, SESSION_OK)
    const walletsBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent === 'Wallets',
    )!
    expect(walletsBtn).toBeTruthy()
    await act(async () => {
      walletsBtn.click()
    })
    expect(container.querySelector('[data-testid="wallets-page"]')).toBeTruthy()
    expect(container.querySelector('[data-testid="dashboard-page"]')).toBeNull()
  })

  // ---- Tab persistence across reload (AUD-350) ----

  it('writes the active tab into the URL hash on tab click', async () => {
    mountWithCache({ setup_required: false }, SESSION_OK)
    const walletsBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent === 'Wallets',
    )!
    await act(async () => {
      walletsBtn.click()
    })
    expect(window.location.hash).toBe('#/wallets')
  })

  it('restores the tab from the URL hash on mount, as a reload would', () => {
    window.history.replaceState(null, '', '#/wallets')
    mountWithCache({ setup_required: false }, SESSION_OK)
    expect(container.querySelector('[data-testid="wallets-page"]')).toBeTruthy()
    expect(container.querySelector('[data-testid="dashboard-page"]')).toBeNull()
  })

  it('falls back to the dashboard for an unknown hash', () => {
    window.history.replaceState(null, '', '#/not-a-page')
    mountWithCache({ setup_required: false }, SESSION_OK)
    expect(container.querySelector('[data-testid="dashboard-page"]')).toBeTruthy()
    expect(window.location.hash).toBe('#/dashboard')
  })

  it('falls back to the dashboard for the retired #/holdings hash', () => {
    // AUD-405 retired the standalone Holdings page; a stale bookmark or a
    // persisted #/holdings hash from before the change must not render blank.
    window.history.replaceState(null, '', '#/holdings')
    mountWithCache({ setup_required: false }, SESSION_OK)
    expect(container.querySelector('[data-testid="dashboard-page"]')).toBeTruthy()
    expect(window.location.hash).toBe('#/dashboard')
  })

  it('follows browser back/forward via hashchange', async () => {
    mountWithCache({ setup_required: false }, SESSION_OK)
    await act(async () => {
      window.location.hash = '#/assets'
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    expect(container.querySelector('[data-testid="assets-page"]')).toBeTruthy()

    await act(async () => {
      window.location.hash = '#/dashboard'
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    expect(container.querySelector('[data-testid="dashboard-page"]')).toBeTruthy()
  })

  it('transitions from SetupPage to SignInPage after setup completes', async () => {
    mockSession.mockRejectedValue(new AuthError())
    mountWithCache({ setup_required: true })
    expect(container.querySelector('[data-testid="setup-page"]')).toBeTruthy()

    const completeBtn = container.querySelector('button')!
    await act(async () => {
      completeBtn.click()
    })
    // After handleSetupComplete sets setup_required: false, session query runs + fails → SignInPage
    await vi.waitFor(
      () => expect(container.querySelector('[data-testid="sign-in-page"]')).toBeTruthy(),
      { timeout: 1000 },
    )
  })

  it('transitions from SignInPage to main app after sign-in', async () => {
    mockSession
      .mockRejectedValueOnce(new AuthError())
      .mockResolvedValue(SESSION_OK)
    const qc = makeQueryClient()
    qc.setQueryData(['setup-status'], { setup_required: false })
    await act(async () => {
      root.render(
        React.createElement(QueryClientProvider, { client: qc }, React.createElement(App)),
      )
    })
    await vi.waitFor(
      () => expect(container.querySelector('[data-testid="sign-in-page"]')).toBeTruthy(),
      { timeout: 1000 },
    )

    const signInBtn = container.querySelector('button')!
    await act(async () => {
      signInBtn.click()
    })
    await vi.waitFor(
      () => expect(container.querySelector('[data-testid="dashboard-page"]')).toBeTruthy(),
      { timeout: 1000 },
    )
  })

  it('calls logout and returns to SignInPage on sign-out', async () => {
    // Pre-populate session so initial render is synchronous; any real fetch (post sign-out) fails
    mockSession.mockRejectedValue(new AuthError())
    const qc = makeQueryClient()
    qc.setQueryData(['setup-status'], { setup_required: false })
    qc.setQueryData(['session'], SESSION_OK)
    await act(async () => {
      root.render(
        React.createElement(QueryClientProvider, { client: qc }, React.createElement(App)),
      )
    })
    expect(container.querySelector('[data-testid="dashboard-page"]')).toBeTruthy()

    const avatarBtn = container.querySelector('[aria-label="Account menu"]') as HTMLButtonElement
    await act(async () => {
      avatarBtn.click()
    })
    const signOutBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent === 'Sign out',
    )!
    await act(async () => {
      signOutBtn.click()
    })
    expect(mockLogout).toHaveBeenCalledOnce()
    await vi.waitFor(
      () => expect(container.querySelector('[data-testid="sign-in-page"]')).toBeTruthy(),
      { timeout: 1000 },
    )
  })

  it('registers a global unauthorized callback on mount', async () => {
    mountWithCache({ setup_required: false }, SESSION_OK)
    expect(mockSetUnauthorizedCallback).toHaveBeenCalledWith(expect.any(Function))
  })

  it('global 401 callback resets session and returns to sign-in', async () => {
    mockSession.mockRejectedValue(new AuthError())
    const qc = makeQueryClient()
    qc.setQueryData(['setup-status'], { setup_required: false })
    qc.setQueryData(['session'], SESSION_OK)
    await act(async () => {
      root.render(
        React.createElement(QueryClientProvider, { client: qc }, React.createElement(App)),
      )
    })
    expect(container.querySelector('[data-testid="dashboard-page"]')).toBeTruthy()

    // Invoke the registered callback as if a 401 fired from any API call
    const callback = mockSetUnauthorizedCallback.mock.calls[0]?.[0]
    expect(callback).toBeDefined()
    await act(async () => {
      callback?.()
    })
    await vi.waitFor(
      () => expect(container.querySelector('[data-testid="sign-in-page"]')).toBeTruthy(),
      { timeout: 1000 },
    )
  })

  it('unauthenticated boot fetches session exactly once — no infinite loop', async () => {
    mockSession.mockRejectedValue(new AuthError())
    const qc = makeQueryClient()
    qc.setQueryData(['setup-status'], { setup_required: false })

    // Capture the registered callback so we can invoke it directly below
    let capturedCallback: (() => void) | undefined
    mockSetUnauthorizedCallback.mockImplementationOnce((cb: () => void) => {
      capturedCallback = cb
    })

    await act(async () => {
      root.render(
        React.createElement(QueryClientProvider, { client: qc }, React.createElement(App)),
      )
    })
    await vi.waitFor(
      () => expect(container.querySelector('[data-testid="sign-in-page"]')).toBeTruthy(),
      { timeout: 1000 },
    )

    // Exactly one session fetch; retry is disabled for AuthError
    expect(mockSession).toHaveBeenCalledTimes(1)

    // Simulating a 401 callback while session is in error state must be a no-op
    // (the guard prevents a reset when status !== 'success')
    expect(capturedCallback).toBeDefined()
    await act(async () => {
      capturedCallback?.()
    })
    // Still exactly one fetch — no second reset/refetch triggered
    expect(mockSession).toHaveBeenCalledTimes(1)
  })

  it('unauthorized callback is no-op when session is already in error state', async () => {
    // Session is pre-populated as success so initial render is synchronous,
    // then any real fetch after a reset will fail with AuthError
    mockSession.mockRejectedValue(new AuthError())
    const qc = makeQueryClient()
    qc.setQueryData(['setup-status'], { setup_required: false })
    qc.setQueryData(['session'], SESSION_OK)

    let capturedCallback: (() => void) | undefined
    mockSetUnauthorizedCallback.mockImplementationOnce((cb: () => void) => {
      capturedCallback = cb
    })

    await act(async () => {
      root.render(
        React.createElement(QueryClientProvider, { client: qc }, React.createElement(App)),
      )
    })
    expect(container.querySelector('[data-testid="dashboard-page"]')).toBeTruthy()

    // First callback invocation: session is 'success' → triggers reset → session enters error
    await act(async () => { capturedCallback?.() })
    await vi.waitFor(
      () => expect(container.querySelector('[data-testid="sign-in-page"]')).toBeTruthy(),
      { timeout: 1000 },
    )
    const fetchCountAfterFirst = mockSession.mock.calls.length

    // Second callback invocation: session is now 'error' → guard blocks → no additional fetches
    await act(async () => { capturedCallback?.() })
    await new Promise((r) => setTimeout(r, 50))
    expect(mockSession).toHaveBeenCalledTimes(fetchCountAfterFirst)
  })
})
