import { describe, it, expect, vi, afterEach } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../api/client', () => {
  class ApiError extends Error {
    status: number
    body: undefined
    constructor(status: number, message: string) {
      super(message)
      this.name = 'ApiError'
      this.status = status
    }
  }
  return {
    fetchWallets: vi.fn(),
    addWallet: vi.fn(),
    patchWallet: vi.fn(),
    triggerJob: vi.fn(),
    ApiError,
  }
})

import WalletsPage from '../pages/WalletsPage'
import { fetchWallets, patchWallet } from '../api/client'
import type { WalletItem } from '../api/client'

const mockFetchWallets = vi.mocked(fetchWallets)
const mockPatchWallet = vi.mocked(patchWallet)

const WALLET_1: WalletItem = {
  id: 'w1',
  address: '0xABCDEF1234567890ABCDef1234567890abcdef12',
  label: 'My Cold Wallet',
  chain_id: 1,
  tracking_active: true,
  coverage: null,
  created_at: '2026-01-01T00:00:00Z',
}

function makeWalletsResponse(items = [WALLET_1], next_cursor: string | null = null) {
  return {
    items,
    next_cursor,
    request_id: 'r1',
    generated_at: '2026-01-01T00:00:00Z',
  }
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
        React.createElement(WalletsPage),
      ),
    )
  })
  return { container, root }
}

describe('WalletsPage', () => {
  let container: HTMLDivElement
  let root: Root

  afterEach(async () => {
    await act(async () => {
      root.unmount()
    })
    document.body.removeChild(container)
    vi.clearAllMocks()
  })

  it('renders wallet list', async () => {
    mockFetchWallets.mockResolvedValue(makeWalletsResponse())
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(container.textContent).toContain('My Cold Wallet'))
  })

  it('sends null when label is empty on submit', async () => {
    // Wallet with no label — edit input starts empty; submitting should send null
    const noLabelWallet = { ...WALLET_1, label: null }
    mockFetchWallets.mockResolvedValue(makeWalletsResponse([noLabelWallet]))
    mockPatchWallet.mockResolvedValue(noLabelWallet)
    ;({ container, root } = mountPage())

    await vi.waitFor(() => expect(container.textContent).toContain('(no label)'))

    // Click Edit button
    const editBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent === 'Edit',
    )!
    await act(async () => { editBtn.click() })

    // Input starts empty; submit the form immediately
    const form = container.querySelector('form[aria-label="Edit label"]') as HTMLFormElement
    expect(form).toBeTruthy()
    await act(async () => { form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })) })

    expect(mockPatchWallet).toHaveBeenCalledWith('w1', { label: null })
  })

  it('does not show Load more button when next_cursor is null', async () => {
    mockFetchWallets.mockResolvedValue(makeWalletsResponse([WALLET_1], null))
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(container.textContent).toContain('My Cold Wallet'))
    const loadMore = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent === 'Load more',
    )
    expect(loadMore).toBeUndefined()
  })

  it('shows Load more button when next_cursor is non-null', async () => {
    mockFetchWallets.mockResolvedValueOnce(makeWalletsResponse([WALLET_1], 'cursor-abc'))
    ;({ container, root } = mountPage())
    await vi.waitFor(() => {
      const btn = Array.from(container.querySelectorAll('button')).find(
        (b) => b.textContent === 'Load more',
      )
      expect(btn).toBeTruthy()
    })
  })

  it('fetches next page when Load more is clicked', async () => {
    const WALLET_2 = { ...WALLET_1, id: 'w2', address: '0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB', label: 'Hot wallet' }
    mockFetchWallets
      .mockResolvedValueOnce(makeWalletsResponse([WALLET_1], 'cursor-abc'))
      .mockResolvedValueOnce(makeWalletsResponse([WALLET_2], null))
    ;({ container, root } = mountPage())

    await vi.waitFor(() => {
      const btn = Array.from(container.querySelectorAll('button')).find(
        (b) => b.textContent === 'Load more',
      )
      expect(btn).toBeTruthy()
    })

    const loadMoreBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent === 'Load more',
    )!
    await act(async () => { loadMoreBtn.click() })

    await vi.waitFor(() => expect(container.textContent).toContain('Hot wallet'))
    expect(mockFetchWallets).toHaveBeenCalledTimes(2)
    expect(mockFetchWallets).toHaveBeenNthCalledWith(2, 'cursor-abc')
  })
})
