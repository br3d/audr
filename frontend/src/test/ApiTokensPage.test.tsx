import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../api/client', async () => {
  const { MockApiError } = await import('./mockApiError')
  return {
    fetchApiTokens: vi.fn(),
    createApiToken: vi.fn(),
    revokeApiToken: vi.fn(),
    ApiError: MockApiError,
  }
})

import ApiTokensPage from '../pages/ApiTokensPage'
import { fetchApiTokens, createApiToken, revokeApiToken } from '../api/client'
import type { ApiTokenItem } from '../api/client'

const mockFetchApiTokens = vi.mocked(fetchApiTokens)
const mockCreateApiToken = vi.mocked(createApiToken)
const mockRevokeApiToken = vi.mocked(revokeApiToken)

// Use the native setter so React's synthetic onChange fires in jsdom.
function nativeSetValue(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
  setter?.call(input, value)
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

const NOW = new Date('2026-06-15T00:00:00Z')

const ACTIVE_TOKEN: ApiTokenItem = {
  id: 't1',
  name: 'claude-desktop',
  scopes: 'read',
  expires_at: '2026-09-01T00:00:00Z',
  created_at: '2026-06-01T00:00:00Z',
  last_used_at: null,
  redact_addresses: false,
}

const EXPIRED_TOKEN: ApiTokenItem = {
  id: 't2',
  name: 'old-script',
  scopes: 'read',
  expires_at: '2026-01-01T00:00:00Z',
  created_at: '2025-10-01T00:00:00Z',
  last_used_at: '2025-12-01T00:00:00Z',
  redact_addresses: false,
}

function mountPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  })
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  act(() => {
    root.render(
      React.createElement(QueryClientProvider, { client: qc },
        React.createElement(ApiTokensPage),
      ),
    )
  })
  return { container, root }
}

describe('ApiTokensPage', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW)
  })

  afterEach(async () => {
    await act(async () => {
      root.unmount()
    })
    document.body.removeChild(container)
    vi.clearAllMocks()
    vi.useRealTimers()
  })

  it('shows an explanatory empty state mentioning LLM clients and full read access', async () => {
    mockFetchApiTokens.mockResolvedValue({ items: [] })
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(container.textContent).toMatch(/no.*tokens/i))
    expect(container.textContent).toMatch(/claude|chatgpt/i)
    expect(container.textContent).toMatch(/read.*(entire|whole|all).*portfolio|portfolio.*read/i)
  })

  it('renders last_used_at as "Never used" when null', async () => {
    mockFetchApiTokens.mockResolvedValue({ items: [ACTIVE_TOKEN] })
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(container.textContent).toContain('claude-desktop'))
    expect(container.textContent).toMatch(/never used/i)
  })

  it('marks an active token as Active and an expired token as Expired', async () => {
    mockFetchApiTokens.mockResolvedValue({ items: [ACTIVE_TOKEN, EXPIRED_TOKEN] })
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(container.textContent).toContain('old-script'))

    const rows = Array.from(container.querySelectorAll('tbody tr'))
    const activeRow = rows.find((r) => r.textContent?.includes('claude-desktop'))!
    const expiredRow = rows.find((r) => r.textContent?.includes('old-script'))!

    expect(activeRow.textContent).toMatch(/active/i)
    expect(activeRow.textContent).not.toMatch(/expired/i)
    expect(expiredRow.textContent).toMatch(/expired/i)
  })

  it('reveals the plaintext token only once on creation and does not restore it after remount', async () => {
    mockFetchApiTokens.mockResolvedValue({ items: [] })
    mockCreateApiToken.mockResolvedValue({
      ...ACTIVE_TOKEN,
      token: 'audr_pat_secretvalue',
    })
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(container.textContent).toMatch(/no.*tokens/i))

    const openBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent === 'Create token',
    )!
    await act(async () => { openBtn.click() })

    const nameInput = container.querySelector('#token-name') as HTMLInputElement
    await act(async () => { nativeSetValue(nameInput, 'claude-desktop') })

    const form = container.querySelector('form') as HTMLFormElement
    await act(async () => { form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })) })

    await vi.waitFor(() => {
      const tokenInput = container.querySelector('input[aria-label="New API token"]') as HTMLInputElement | null
      expect(tokenInput?.value).toBe('audr_pat_secretvalue')
    })

    // Simulate a page reload: unmount and remount a fresh page instance.
    await act(async () => { root.unmount() })
    document.body.removeChild(container)
    ;({ container, root } = mountPage())

    await vi.waitFor(() => expect(mockFetchApiTokens).toHaveBeenCalled())
    expect(container.textContent).not.toContain('audr_pat_secretvalue')
  })

  it('requires confirmation before revoking and refreshes the list immediately after', async () => {
    mockFetchApiTokens
      .mockResolvedValueOnce({ items: [ACTIVE_TOKEN] })
      .mockResolvedValueOnce({ items: [] })
    mockRevokeApiToken.mockResolvedValue(undefined)
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(container.textContent).toContain('claude-desktop'))

    const revokeBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent === 'Revoke',
    )!
    await act(async () => { revokeBtn.click() })

    expect(mockRevokeApiToken).not.toHaveBeenCalled()
    const confirmBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent === 'Confirm revoke',
    )!
    await act(async () => { confirmBtn.click() })

    expect(mockRevokeApiToken).toHaveBeenCalledWith('t1')
    await vi.waitFor(() => expect(container.textContent).not.toContain('claude-desktop'))
  })
})
