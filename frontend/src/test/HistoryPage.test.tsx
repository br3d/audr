import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
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
    fetchHistory: vi.fn(),
    ApiError,
  }
})

import HistoryPage from '../pages/HistoryPage'
import { fetchHistory } from '../api/client'
import type { HistoryResponse, HistoryPoint } from '../api/client'

const mockFetchHistory = vi.mocked(fetchHistory)

function makeHistoryResponse(overrides: Partial<HistoryResponse> = {}): HistoryResponse {
  return { period: '7d', entries: [], next_cursor: null, ...overrides }
}

function makePoint(overrides: Partial<HistoryPoint> = {}): HistoryPoint {
  return {
    snapshot_id: 'snap-1',
    snapshotted_at: '2026-01-15T12:00:00Z',
    total_value_usd: '5000.00',
    quality: 'complete',
    included_wallet_count: 1,
    included_asset_count: 1,
    has_gap: false,
    is_canonical: true,
    is_gap_marker: false,
    ...overrides,
  }
}

function mountPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  act(() => {
    root.render(
      React.createElement(QueryClientProvider, { client: qc }, React.createElement(HistoryPage)),
    )
  })
  return { container, root }
}

describe('HistoryPage — period persistence in the URL (AUD-353)', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    window.history.replaceState(null, '', '/')
    mockFetchHistory.mockResolvedValue(makeHistoryResponse())
  })

  afterEach(async () => {
    await act(async () => {
      root.unmount()
    })
    document.body.removeChild(container)
    vi.clearAllMocks()
  })

  it('defaults to the 7d range and requests it', async () => {
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(mockFetchHistory).toHaveBeenCalledWith('7d'))
    const activeTab = container.querySelector('.range-tab.active')
    expect(activeTab?.textContent).toBe('7d')
  })

  it('restores the selected range from the URL on mount, as a reload would (AC1)', async () => {
    window.history.replaceState(null, '', '#/history?period=30d')
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(mockFetchHistory).toHaveBeenCalledWith('30d'))
    const activeTab = container.querySelector('.range-tab.active')
    expect(activeTab?.textContent).toBe('30d')
  })

  it('writes the selected range into the URL hash on click', async () => {
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(mockFetchHistory).toHaveBeenCalled())

    const allTimeBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent === 'All time',
    )!
    await act(async () => {
      allTimeBtn.click()
    })

    expect(window.location.hash).toBe('#/history?period=all')
    await vi.waitFor(() => expect(mockFetchHistory).toHaveBeenCalledWith('all'))
  })

  it('falls back to the default range for an unknown period value, without crashing (AC3)', async () => {
    window.history.replaceState(null, '', '#/history?period=decades')
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(mockFetchHistory).toHaveBeenCalledWith('7d'))
    expect(container.querySelector('.range-tab.active')?.textContent).toBe('7d')
  })

  it('offers six range options including 90d and 1 year (AUD-376)', async () => {
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(mockFetchHistory).toHaveBeenCalled())
    const labels = Array.from(container.querySelectorAll('.range-tab')).map((b) => b.textContent)
    expect(labels).toEqual(['24h', '7d', '30d', '90d', '1 year', 'All time'])
  })

  it('selecting 90d writes it into the URL hash and requests it', async () => {
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(mockFetchHistory).toHaveBeenCalled())
    const btn90d = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent === '90d',
    )!
    await act(async () => {
      btn90d.click()
    })
    expect(window.location.hash).toBe('#/history?period=90d')
    await vi.waitFor(() => expect(mockFetchHistory).toHaveBeenCalledWith('90d'))
  })

  it('selecting 1 year writes it into the URL hash and requests it', async () => {
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(mockFetchHistory).toHaveBeenCalled())
    const btn1y = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent === '1 year',
    )!
    await act(async () => {
      btn1y.click()
    })
    expect(window.location.hash).toBe('#/history?period=1y')
    await vi.waitFor(() => expect(mockFetchHistory).toHaveBeenCalledWith('1y'))
  })
})

describe('HistoryPage — invalidation explanation (AUD-86)', () => {
  let container: HTMLDivElement
  let root: Root

  afterEach(async () => {
    await act(async () => {
      root.unmount()
    })
    document.body.removeChild(container)
    vi.clearAllMocks()
  })

  it('shows an invalidation notice when the range contains non-canonical points', async () => {
    mockFetchHistory.mockResolvedValue(
      makeHistoryResponse({ entries: [makePoint({ is_canonical: false })] }),
    )
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(mockFetchHistory).toHaveBeenCalled())
    await vi.waitFor(() => {
      expect(container.textContent?.toLowerCase()).toContain('invalidated')
    })
    const notice = Array.from(container.querySelectorAll('[role="note"]')).find((el) =>
      el.textContent?.toLowerCase().includes('invalidated'),
    )
    expect(notice).toBeTruthy()
    expect(notice?.textContent?.toLowerCase()).toMatch(/reorg|verification/)
  })

  it('does not show an invalidation notice when all points are canonical', async () => {
    mockFetchHistory.mockResolvedValue(
      makeHistoryResponse({ entries: [makePoint({ is_canonical: true })] }),
    )
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(mockFetchHistory).toHaveBeenCalled())
    await vi.waitFor(() => {
      expect(container.querySelector('.card')).toBeTruthy()
    })
    expect(container.textContent?.toLowerCase()).not.toContain('invalidated')
  })
})
