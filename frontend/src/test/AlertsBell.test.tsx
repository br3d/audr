import { describe, it, expect, vi, afterEach } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../api/client', async () => {
  const { MockApiError } = await import('./mockApiError')
  return {
    fetchAlerts: vi.fn(),
    markAlertRead: vi.fn(),
    ApiError: MockApiError,
  }
})

import AlertsBell from '../components/AlertsBell'
import { fetchAlerts, markAlertRead } from '../api/client'
import type { AlertItem, AlertsResponse } from '../api/client'

const mockFetchAlerts = vi.mocked(fetchAlerts)
const mockMarkAlertRead = vi.mocked(markAlertRead)

function makeAlert(overrides: Partial<AlertItem> = {}): AlertItem {
  return {
    id: 'a1',
    asset_news_id: 'n1',
    asset_id: 'eth',
    category: 'market_move',
    severity: 'warning',
    summary: 'ETH dropped 12% in an hour.',
    classifier: 'heuristic',
    model: null,
    created_at: '2026-01-01T00:00:00Z',
    read_at: null,
    // A `warning` is never pushed outside the app (ADR-007 §2), so undispatched
    // is the right default for this fixture (AUD-595).
    dispatched_at: null,
    dispatched_via: null,
    asset_symbol: 'ETH',
    article_title: 'Bridge exploited for $40M',
    article_url: 'https://example.com/a',
    ...overrides,
  }
}

function makeResponse(alerts: AlertItem[] = []): AlertsResponse {
  return {
    total: alerts.length,
    unread_count: alerts.filter((a) => a.read_at === null).length,
    limit: 20,
    offset: 0,
    alerts,
  }
}

function render(ui: React.ReactElement): { container: HTMLDivElement; root: Root; qc: QueryClient } {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  act(() => {
    root.render(React.createElement(QueryClientProvider, { client: qc }, ui))
  })
  return { container, root, qc }
}

async function unmount(container: HTMLDivElement, root: Root) {
  await act(async () => { root.unmount() })
  document.body.removeChild(container)
}

async function flush() {
  for (let i = 0; i < 5; i += 1) {
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)) })
  }
}

describe('AlertsBell', () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  describe('badge', () => {
    it('shows the unread count on the badge', async () => {
      mockFetchAlerts.mockResolvedValue(makeResponse([makeAlert(), makeAlert({ id: 'a2' })]))
      const { container, root } = render(<AlertsBell />)
      await flush()
      expect(container.querySelector('.alerts-bell-badge')?.textContent).toBe('2')
      await unmount(container, root)
    })

    it('renders no badge when unread_count is 0', async () => {
      mockFetchAlerts.mockResolvedValue(
        makeResponse([makeAlert({ read_at: '2026-01-01T01:00:00Z' })]),
      )
      const { container, root } = render(<AlertsBell />)
      await flush()
      expect(container.querySelector('.alerts-bell-badge')).toBeNull()
      await unmount(container, root)
    })
  })

  describe('dropdown', () => {
    it('shows an explanatory empty state when there are no alerts', async () => {
      mockFetchAlerts.mockResolvedValue(makeResponse([]))
      const { container, root } = render(<AlertsBell />)
      await flush()
      await act(async () => {
        ;(container.querySelector('.alerts-bell-button') as HTMLButtonElement).click()
      })
      expect(container.querySelector('.alerts-bell-empty')?.textContent).toBe('No alerts yet.')
      await unmount(container, root)
    })

    it('tells two alerts apart by symbol and headline, not by summary', async () => {
      // Both carry the identical classifier reason — which is exactly why the
      // bell used to read as a list of duplicates (AUD-683).
      mockFetchAlerts.mockResolvedValue(
        makeResponse([
          makeAlert({ asset_symbol: 'ETH', article_title: 'Drift exploit drains $61M' }),
          makeAlert({ id: 'a2', asset_symbol: 'UNI', article_title: 'Uniswap hook drains LPs' }),
        ]),
      )
      const { container, root } = render(<AlertsBell />)
      await flush()
      await act(async () => {
        ;(container.querySelector('.alerts-bell-button') as HTMLButtonElement).click()
      })
      const summaries = Array.from(
        container.querySelectorAll('.alerts-bell-item-summary'),
      ).map((el) => el.textContent)
      expect(summaries).toEqual(['ETHDrift exploit drains $61M', 'UNIUniswap hook drains LPs'])
      await unmount(container, root)
    })

    it('links out to the article behind the alert', async () => {
      mockFetchAlerts.mockResolvedValue(
        makeResponse([makeAlert({ article_url: 'https://example.com/story' })]),
      )
      const { container, root } = render(<AlertsBell />)
      await flush()
      await act(async () => {
        ;(container.querySelector('.alerts-bell-button') as HTMLButtonElement).click()
      })
      const link = container.querySelector('.alerts-bell-item-link') as HTMLAnchorElement
      expect(link.href).toBe('https://example.com/story')
      expect(link.rel).toBe('noopener noreferrer')
      await unmount(container, root)
    })

    it('falls back to the summary when the headline is missing', async () => {
      mockFetchAlerts.mockResolvedValue(
        makeResponse([makeAlert({ article_title: null, article_url: null, asset_symbol: null })]),
      )
      const { container, root } = render(<AlertsBell />)
      await flush()
      await act(async () => {
        ;(container.querySelector('.alerts-bell-button') as HTMLButtonElement).click()
      })
      expect(container.querySelector('.alerts-bell-item-summary')?.textContent).toBe(
        'ETH dropped 12% in an hour.',
      )
      expect(container.querySelector('.alerts-bell-item-link')).toBeNull()
      await unmount(container, root)
    })

    it('clicking an unread alert marks it as read', async () => {
      mockFetchAlerts.mockResolvedValue(makeResponse([makeAlert()]))
      mockMarkAlertRead.mockResolvedValue({ id: 'a1', read_at: '2026-01-01T02:00:00Z' })
      const { container, root } = render(<AlertsBell />)
      await flush()
      await act(async () => {
        ;(container.querySelector('.alerts-bell-button') as HTMLButtonElement).click()
      })
      await act(async () => {
        ;(container.querySelector('.alerts-bell-item-button') as HTMLButtonElement).click()
      })
      await flush()
      expect(mockMarkAlertRead).toHaveBeenCalledWith('a1')
      await unmount(container, root)
    })
  })
})
