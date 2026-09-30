import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { PortfolioResponse } from '../api/client'

vi.mock('../api/client', () => ({
  fetchPortfolio: vi.fn(),
  fetchHistory: vi.fn(),
  ApiError: class ApiError extends Error {
    status = 500
    body = undefined
    constructor(msg = 'API error') {
      super(msg)
      this.name = 'ApiError'
    }
  },
}))

vi.mock('../components/HistoryChart', () => ({
  default: () => null,
}))

vi.mock('../components/NewsFeed', () => ({
  default: ({ items }: { items: { asset_id: string }[] }) =>
    React.createElement('div', { 'aria-label': 'News feed stub' }, `${items.length} assets`),
}))

import DashboardPage from '../pages/DashboardPage'
import { fetchPortfolio, fetchHistory } from '../api/client'

const mockFetchPortfolio = vi.mocked(fetchPortfolio)
const mockFetchHistory = vi.mocked(fetchHistory)

function makeQuality(overrides: Partial<PortfolioResponse['quality']> = {}): PortfolioResponse['quality'] {
  return {
    incomplete: false,
    stale_balances: false,
    stale_prices: false,
    mixed_observation_times: false,
    discovery_overdue: false,
    verification_pending: false,
    invalidated: false,
    ...overrides,
  }
}

const EMPTY_PORTFOLIO: PortfolioResponse = {
  snapshot_id: null,
  membership_revision: null,
  valuation_time: null,
  currency: 'USD',
  priced_subtotal_usd: null,
  total_usd: null,
  quality: makeQuality(),
  balance_block: null,
  balance_block_time: null,
  balance_observed_at: null,
  discovery_completed_at: null,
  holdings: [],
  allocations: [],
  stale_contribution_usd: null,
  request_id: 'req-1',
  generated_at: '2026-01-01T00:00:00Z',
}

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  })
}

function mountWithData(portfolio: PortfolioResponse): {
  container: HTMLDivElement
  root: Root
  qc: QueryClient
} {
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  const qc = makeQueryClient()
  qc.setQueryData(['portfolio'], portfolio)
  act(() => {
    root.render(
      React.createElement(
        QueryClientProvider,
        { client: qc },
        React.createElement(DashboardPage),
      ),
    )
  })
  return { container, root, qc }
}

async function unmount(container: HTMLDivElement, root: Root) {
  await act(async () => { root.unmount() })
  document.body.removeChild(container)
}

const EMPTY_HISTORY = { period: '30d' as const, entries: [], next_cursor: null }

describe('DashboardPage', () => {
  beforeEach(() => {
    mockFetchPortfolio.mockResolvedValue(EMPTY_PORTFOLIO)
    mockFetchHistory.mockResolvedValue(EMPTY_HISTORY)
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  describe('total display', () => {
    it('shows empty-state note when both total_usd and priced_subtotal_usd are null', async () => {
      const { container, root } = mountWithData(EMPTY_PORTFOLIO)
      expect(container.textContent).toMatch(/no priced holdings/i)
      await unmount(container, root)
    })

    it('shows complete total with "$" when total_usd is present', async () => {
      const { container, root } = mountWithData({
        ...EMPTY_PORTFOLIO,
        total_usd: '4567.89',
        priced_subtotal_usd: '4567.89',
      })
      expect(container.textContent).toContain('$4,567.89')
      expect(container.textContent).toContain('Portfolio Value')
      expect(container.textContent).not.toContain('incomplete')
      await unmount(container, root)
    })

    it('shows subtotal with "(incomplete)" label when only priced_subtotal_usd is set', async () => {
      const { container, root } = mountWithData({
        ...EMPTY_PORTFOLIO,
        total_usd: null,
        priced_subtotal_usd: '1234.00',
        quality: makeQuality({ incomplete: true }),
      })
      expect(container.textContent).toContain('$1,234.00')
      expect(container.textContent).toContain('incomplete')
      await unmount(container, root)
    })

    it('shows stale contribution note when stale_contribution_usd is set', async () => {
      const { container, root } = mountWithData({
        ...EMPTY_PORTFOLIO,
        total_usd: '500.00',
        priced_subtotal_usd: '500.00',
        stale_contribution_usd: '100.00',
      })
      expect(container.textContent).toMatch(/stale/i)
      expect(container.textContent).toContain('$100.00')
      await unmount(container, root)
    })
  })

  describe('quality notices', () => {
    it('shows stale_prices notice when quality flag is set', async () => {
      const { container, root } = mountWithData({
        ...EMPTY_PORTFOLIO,
        quality: makeQuality({ stale_prices: true }),
      })
      expect(container.textContent).toMatch(/price data is stale/i)
      await unmount(container, root)
    })

    it('shows stale_balances notice when quality flag is set', async () => {
      const { container, root } = mountWithData({
        ...EMPTY_PORTFOLIO,
        quality: makeQuality({ stale_balances: true }),
      })
      expect(container.textContent).toMatch(/balance reads are stale/i)
      await unmount(container, root)
    })

    it('shows no quality notices when all flags are false', async () => {
      const { container, root } = mountWithData(EMPTY_PORTFOLIO)
      expect(container.querySelector('[aria-label="Data quality notices"]')).toBeNull()
      await unmount(container, root)
    })
  })

  describe('allocation section', () => {
    it('shows allocation unavailable note when allocations array is empty and holdings exist', async () => {
      const { container, root } = mountWithData({
        ...EMPTY_PORTFOLIO,
        holdings: [
          {
            wallet_id: 'w1',
            asset_id: 'eth',
            contract_address: null,
            is_native: true,
            raw_balance: '1000000000000000000',
            decimals: 18,
            quantity: '1.0',
            price_usd: null,
            value_usd: null,
            included: true,
            metadata_source: 'chain',
            read_status: 'ok',
            block_time: null,
            observed_at: null,
            last_success_at: null,
          },
        ],
        allocations: [],
      })
      expect(container.textContent).toMatch(/no priced holdings/i)
      await unmount(container, root)
    })

    it('renders allocation section when allocations are present', async () => {
      const { container, root } = mountWithData({
        ...EMPTY_PORTFOLIO,
        total_usd: '4000.00',
        priced_subtotal_usd: '4000.00',
        allocations: [
          { asset_id: 'eth', symbol: 'ETH', value_usd: '4000.00', percentage: '100.00' },
        ],
      })
      expect(container.querySelector('[aria-label="Asset allocation"]')).toBeTruthy()
      expect(container.textContent).toContain('ETH')
      await unmount(container, root)
    })

    it('does not render allocation chart when allocations are empty', async () => {
      const { container, root } = mountWithData(EMPTY_PORTFOLIO)
      // Chart would have aria-label="Asset allocation pie chart"
      expect(
        container.querySelector('[aria-label="Asset allocation pie chart"]'),
      ).toBeNull()
      await unmount(container, root)
    })
  })

  describe('news section', () => {
    it('renders the News section wired to priced allocations', async () => {
      const { container, root } = mountWithData({
        ...EMPTY_PORTFOLIO,
        total_usd: '4000.00',
        priced_subtotal_usd: '4000.00',
        allocations: [
          { asset_id: 'eth', symbol: 'ETH', value_usd: '4000.00', percentage: '100.00' },
        ],
      })
      expect(container.querySelector('[aria-label="Asset news"]')).toBeTruthy()
      expect(container.querySelector('[aria-label="News feed stub"]')?.textContent).toBe(
        '1 assets',
      )
      await unmount(container, root)
    })

    it('does not render the News section when there are no priced allocations', async () => {
      const { container, root } = mountWithData(EMPTY_PORTFOLIO)
      expect(container.querySelector('[aria-label="Asset news"]')).toBeNull()
      await unmount(container, root)
    })
  })

  describe('excluded-only state', () => {
    it('shows excluded-only note when all holdings are excluded', async () => {
      const { container, root } = mountWithData({
        ...EMPTY_PORTFOLIO,
        holdings: [
          {
            wallet_id: 'w1',
            asset_id: 'eth',
            contract_address: null,
            is_native: true,
            raw_balance: '1000000000000000000',
            decimals: 18,
            quantity: '1.0',
            price_usd: '2000.00',
            value_usd: '2000.00',
            included: false,
            metadata_source: 'chain',
            read_status: 'ok',
            block_time: null,
            observed_at: null,
            last_success_at: null,
          },
        ],
        allocations: [],
      })
      expect(container.textContent).toMatch(/all holdings are excluded/i)
      await unmount(container, root)
    })
  })

  describe('loading and error states', () => {
    it('shows loading indicator while portfolio is pending', async () => {
      mockFetchPortfolio.mockReturnValue(new Promise(() => {}))
      const container = document.createElement('div')
      document.body.appendChild(container)
      const root = createRoot(container)
      const qc = makeQueryClient()
      await act(async () => {
        root.render(
          React.createElement(
            QueryClientProvider,
            { client: qc },
            React.createElement(DashboardPage),
          ),
        )
      })
      expect(container.textContent).toMatch(/loading/i)
      await act(async () => { root.unmount() })
      document.body.removeChild(container)
    })
  })
})
