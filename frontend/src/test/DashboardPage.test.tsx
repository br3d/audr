import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { AllocationItem, Holding, PortfolioResponse, HistoryPoint, HistoryResponse } from '../api/client'

vi.mock('../api/client', () => ({
  fetchPortfolio: vi.fn(),
  fetchHistory: vi.fn(),
  fetchEvents: vi.fn(),
  fetchAssets: vi.fn(),
  patchAsset: vi.fn(),
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
import { fetchPortfolio, fetchHistory, fetchEvents, fetchAssets } from '../api/client'

const mockFetchPortfolio = vi.mocked(fetchPortfolio)
const mockFetchHistory = vi.mocked(fetchHistory)
const mockFetchEvents = vi.mocked(fetchEvents)
const mockFetchAssets = vi.mocked(fetchAssets)

const EMPTY_ASSETS = {
  items: [],
  next_cursor: null,
  excluded_count: 0,
  request_id: 'req-1',
  generated_at: '2026-01-01T00:00:00Z',
}

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

function makeHolding(overrides: Partial<Holding> = {}): Holding {
  return {
    wallet_id: 'w1',
    asset_id: 'eth',
    contract_address: null,
    is_native: true,
    raw_balance: '1000000000000000000',
    decimals: 18,
    quantity: '1.0',
    price_usd: '2000.00',
    value_usd: '2000.00',
    included: true,
    metadata_source: 'chain',
    read_status: 'ok',
    block_time: null,
    observed_at: null,
    last_success_at: null,
    ...overrides,
  }
}

function makeAllocation(overrides: Partial<AllocationItem> = {}): AllocationItem {
  return {
    asset_id: 'eth',
    symbol: 'ETH',
    value_usd: '4000.00',
    percentage: '100.00',
    quantity: '2.0',
    price_usd: '2000.00',
    wallet_count: 1,
    read_status: 'ok',
    included: true,
    ...overrides,
  }
}

const ETH_ALLOCATION = makeAllocation()

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
        React.createElement(DashboardPage, { setPage: vi.fn() }),
      ),
    )
  })
  return { container, root, qc }
}

// Flush the microtask queue so pending React Query fetches (mocked as
// resolved promises) settle and their re-render lands before assertions run.
async function flush() {
  for (let i = 0; i < 10; i++) {
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)) })
  }
}

async function unmount(container: HTMLDivElement, root: Root) {
  await act(async () => { root.unmount() })
  document.body.removeChild(container)
}

const EMPTY_HISTORY = {
  period: '30d' as const,
  items: [],
  next_cursor: null,
  request_id: 'req-1',
  generated_at: '2026-01-01T00:00:00Z',
}
const EMPTY_EVENTS = { total: 0, limit: 7, offset: 0, events: [] }

function historyPoint(overrides: Partial<HistoryPoint> = {}): HistoryPoint {
  return {
    snapshot_id: 'snap',
    snapshotted_at: '2026-01-01T00:00:00Z',
    total_value_usd: null,
    quality: 'complete',
    included_wallet_count: 1,
    included_asset_count: 1,
    has_gap: false,
    is_canonical: true,
    is_gap_marker: false,
    ...overrides,
  }
}

function mockHistoryByPeriod(entries24h: HistoryPoint[]) {
  mockFetchHistory.mockImplementation((period): Promise<HistoryResponse> => {
    if (period === '24h') {
      return Promise.resolve({
        period: '24h',
        items: entries24h,
        next_cursor: null,
        request_id: 'req-1',
        generated_at: '2026-01-01T00:00:00Z',
      })
    }
    return Promise.resolve(EMPTY_HISTORY)
  })
}

describe('DashboardPage', () => {
  beforeEach(() => {
    mockFetchPortfolio.mockResolvedValue(EMPTY_PORTFOLIO)
    mockFetchHistory.mockResolvedValue(EMPTY_HISTORY)
    mockFetchEvents.mockResolvedValue(EMPTY_EVENTS)
    mockFetchAssets.mockResolvedValue(EMPTY_ASSETS)
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

  describe('24h change', () => {
    it('renders no change row when fewer than two priced points exist in the 24h window', async () => {
      mockHistoryByPeriod([historyPoint({ total_value_usd: '100.00' })])
      const { container, root } = mountWithData({
        ...EMPTY_PORTFOLIO,
        total_usd: '100.00',
        priced_subtotal_usd: '100.00',
      })
      expect(container.querySelector('.metric-change-positive')).toBeNull()
      expect(container.querySelector('.metric-change-negative')).toBeNull()
      expect(container.textContent).not.toMatch(/24h/)
      await unmount(container, root)
    })

    it('does not count gap points with a null value as one of the two required points', async () => {
      mockHistoryByPeriod([
        historyPoint({ snapshotted_at: '2026-01-01T00:00:00Z', total_value_usd: '100.00' }),
        historyPoint({ snapshotted_at: '2026-01-01T06:00:00Z', total_value_usd: null, has_gap: true }),
      ])
      const { container, root } = mountWithData({
        ...EMPTY_PORTFOLIO,
        total_usd: '100.00',
        priced_subtotal_usd: '100.00',
      })
      expect(container.textContent).not.toMatch(/24h/)
      await unmount(container, root)
    })

    it('shows a positive change with sign, percent, and the success color class', async () => {
      mockHistoryByPeriod([
        historyPoint({ snapshotted_at: '2026-01-01T00:00:00Z', total_value_usd: '100.00' }),
        historyPoint({ snapshotted_at: '2026-01-01T12:00:00Z', total_value_usd: '110.00' }),
      ])
      const { container, root } = mountWithData({
        ...EMPTY_PORTFOLIO,
        total_usd: '110.00',
        priced_subtotal_usd: '110.00',
      })
      await flush()
      const changeEl = container.querySelector('.metric-change-positive')
      expect(changeEl).toBeTruthy()
      expect(changeEl?.textContent).toContain('+$10.00')
      expect(changeEl?.textContent).toContain('+10.00%')
      expect(container.querySelector('.metric-change-negative')).toBeNull()
      await unmount(container, root)
    })

    it('shows a negative change with sign, percent, and the danger color class', async () => {
      mockHistoryByPeriod([
        historyPoint({ snapshotted_at: '2026-01-01T00:00:00Z', total_value_usd: '200.00' }),
        historyPoint({ snapshotted_at: '2026-01-01T12:00:00Z', total_value_usd: '150.00' }),
      ])
      const { container, root } = mountWithData({
        ...EMPTY_PORTFOLIO,
        total_usd: '150.00',
        priced_subtotal_usd: '150.00',
      })
      await flush()
      const changeEl = container.querySelector('.metric-change-negative')
      expect(changeEl).toBeTruthy()
      expect(changeEl?.textContent).toContain('-$50.00')
      expect(changeEl?.textContent).toContain('-25.00%')
      expect(container.querySelector('.metric-change-positive')).toBeNull()
      await unmount(container, root)
    })
  })

  describe('metric card notes', () => {
    it('always shows a tracked-holdings note, even with zero holdings', async () => {
      const { container, root } = mountWithData(EMPTY_PORTFOLIO)
      expect(container.textContent).toMatch(/no wallets tracked yet/i)
      await unmount(container, root)
    })

    it('shows a priced-count note when some but not all holdings are priced', async () => {
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
          {
            wallet_id: 'w1',
            asset_id: 'usdc',
            contract_address: '0xabc',
            is_native: false,
            raw_balance: '1000000',
            decimals: 6,
            quantity: '1.0',
            price_usd: '1.00',
            value_usd: '1.00',
            included: true,
            metadata_source: 'chain',
            read_status: 'ok',
            block_time: null,
            observed_at: null,
            last_success_at: null,
          },
        ],
      })
      expect(container.textContent).toMatch(/1 of 2 priced/i)
      await unmount(container, root)
    })

    it('always shows a priced-assets note, falling back to "no balance snapshot" when unset', async () => {
      const { container, root } = mountWithData(EMPTY_PORTFOLIO)
      expect(container.textContent).toMatch(/no balance snapshot yet/i)
      await unmount(container, root)
    })

    it('shows an "as of" date note for priced assets when balance_block_time is set', async () => {
      const { container, root } = mountWithData({
        ...EMPTY_PORTFOLIO,
        balance_block_time: '2026-03-15T10:00:00Z',
      })
      expect(container.textContent).toMatch(/as of Mar 15/i)
      await unmount(container, root)
    })
  })

  describe('page header', () => {
    it('renders a breadcrumb, the page title, and a data-freshness subtitle', async () => {
      const { container, root } = mountWithData({
        ...EMPTY_PORTFOLIO,
        balance_block_time: '2026-03-15T10:00:00Z',
        balance_observed_at: '2026-03-15T10:05:00Z',
      })
      expect(container.textContent).toContain('Dashboard')
      expect(container.querySelector('h2')?.textContent).toBe('Overview')
      expect(container.textContent).toMatch(/balances as of/i)
      await unmount(container, root)
    })

    it('shows a fallback subtitle when no balance snapshot exists yet', async () => {
      const { container, root } = mountWithData(EMPTY_PORTFOLIO)
      expect(container.textContent).toMatch(/no balance snapshot yet/i)
      await unmount(container, root)
    })

    it('never renders a raw ISO timestamp on the page', async () => {
      const { container, root } = mountWithData({
        ...EMPTY_PORTFOLIO,
        balance_block_time: '2026-03-15T10:00:00Z',
        balance_observed_at: '2026-03-15T10:05:00Z',
      })
      expect(container.textContent).not.toMatch(/2026-03-15T10:00:00/)
      expect(container.textContent).not.toMatch(/2026-03-15T10:05:00/)
      await unmount(container, root)
    })

    it('calls setPage("wallets") when the + Add Wallet button is clicked', async () => {
      const setPage = vi.fn()
      const container = document.createElement('div')
      document.body.appendChild(container)
      const root = createRoot(container)
      const qc = makeQueryClient()
      qc.setQueryData(['portfolio'], EMPTY_PORTFOLIO)
      act(() => {
        root.render(
          React.createElement(
            QueryClientProvider,
            { client: qc },
            React.createElement(DashboardPage, { setPage }),
          ),
        )
      })
      const button = Array.from(container.querySelectorAll('button')).find(
        (b) => b.textContent === '+ Add Wallet',
      )!
      act(() => { button.click() })
      expect(setPage).toHaveBeenCalledWith('wallets')
      await unmount(container, root)
    })

    // AUD-408: Assets no longer has a sidebar entry, so this header button is
    // the only always-visible way into the token registry.
    it('calls setPage("assets") when the Manage assets button is clicked', async () => {
      const setPage = vi.fn()
      const container = document.createElement('div')
      document.body.appendChild(container)
      const root = createRoot(container)
      const qc = makeQueryClient()
      qc.setQueryData(['portfolio'], EMPTY_PORTFOLIO)
      act(() => {
        root.render(
          React.createElement(
            QueryClientProvider,
            { client: qc },
            React.createElement(DashboardPage, { setPage }),
          ),
        )
      })
      const button = Array.from(container.querySelectorAll('button')).find(
        (b) => b.textContent === 'Manage assets',
      )!
      act(() => { button.click() })
      expect(setPage).toHaveBeenCalledWith('assets')
      await unmount(container, root)
    })
  })

  describe('recent events', () => {
    it('shows an honest empty state when there are no events', async () => {
      const { container, root } = mountWithData(EMPTY_PORTFOLIO)
      expect(container.textContent).toMatch(/no on-chain events indexed yet/i)
      expect(container.textContent).not.toMatch(/coming soon/i)
      await unmount(container, root)
    })

    it('renders real events when present', async () => {
      mockFetchEvents.mockResolvedValue({
        total: 1,
        limit: 7,
        offset: 0,
        events: [
          {
            id: 'ev1',
            wallet_id: 'w1',
            tx_hash: '0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            block_number: 100,
            log_index: 0,
            event_type: 'transfer_in',
            token_address: '0x1234567890abcdef1234567890abcdef12345678',
            from_address: '0xaaaa',
            to_address: '0xbbbb',
            raw_amount: '1000000',
            indexed_at: '2026-01-01T00:00:00Z',
          },
        ],
      })
      const { container, root } = mountWithData(EMPTY_PORTFOLIO)
      await flush()
      expect(container.textContent).not.toMatch(/no on-chain events indexed yet/i)
      expect(container.textContent).not.toMatch(/coming soon/i)
      expect(container.textContent).toContain('in')
      await unmount(container, root)
    })

    it('has no "AI Focus" placeholder on the page', async () => {
      const { container, root } = mountWithData(EMPTY_PORTFOLIO)
      expect(container.textContent).not.toMatch(/AI Focus/i)
      await unmount(container, root)
    })

    it('calls setPage("events") when "View all" is clicked', async () => {
      const setPage = vi.fn()
      const container = document.createElement('div')
      document.body.appendChild(container)
      const root = createRoot(container)
      const qc = makeQueryClient()
      qc.setQueryData(['portfolio'], EMPTY_PORTFOLIO)
      act(() => {
        root.render(
          React.createElement(
            QueryClientProvider,
            { client: qc },
            React.createElement(DashboardPage, { setPage }),
          ),
        )
      })
      const button = Array.from(container.querySelectorAll('button')).find(
        (b) => b.textContent === 'View all',
      )!
      act(() => { button.click() })
      expect(setPage).toHaveBeenCalledWith('events')
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
        holdings: [makeHolding({ price_usd: null, value_usd: null })],
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
        allocations: [ETH_ALLOCATION],
      })
      expect(container.querySelector('[aria-label="Asset allocation"]')).toBeTruthy()
      expect(container.textContent).toContain('ETH')
      await unmount(container, root)
    })

    it('does not render the allocation list when allocations are empty', async () => {
      const { container, root } = mountWithData(EMPTY_PORTFOLIO)
      expect(container.querySelector('[aria-label="Asset allocation"]')).toBeNull()
      await unmount(container, root)
    })

    it('renders the Refresh balances / Discover tokens scan controls in the card header', async () => {
      const { container, root } = mountWithData({
        ...EMPTY_PORTFOLIO,
        total_usd: '4000.00',
        priced_subtotal_usd: '4000.00',
        allocations: [ETH_ALLOCATION],
      })
      expect(
        Array.from(container.querySelectorAll('button')).some(
          (b) => b.textContent === 'Refresh balances',
        ),
      ).toBe(true)
      expect(
        Array.from(container.querySelectorAll('button')).some(
          (b) => b.textContent === 'Discover tokens',
        ),
      ).toBe(true)
      await unmount(container, root)
    })

    it('styles the scan controls with the shared Folio button classes (AUD-415)', async () => {
      const { container, root } = mountWithData({
        ...EMPTY_PORTFOLIO,
        total_usd: '4000.00',
        priced_subtotal_usd: '4000.00',
        allocations: [ETH_ALLOCATION],
      })
      const scanButtons = Array.from(
        container.querySelectorAll('.scan-status button'),
      )
      expect(scanButtons.length).toBe(2)
      for (const btn of scanButtons) {
        expect(btn.className).toContain('btn')
        expect(btn.className).toContain('btn-sm')
        expect(btn.className).toContain('btn-secondary')
        expect(btn.querySelector('svg')).not.toBeNull()
      }
      await unmount(container, root)
    })

    it('still shows the allocation card (not the unavailable alert) when only unpriced assets exist', async () => {
      const { container, root } = mountWithData({
        ...EMPTY_PORTFOLIO,
        holdings: [makeHolding({ asset_id: 'xyz', value_usd: null, price_usd: null })],
        allocations: [
          {
            asset_id: 'xyz',
            symbol: 'XYZ',
            value_usd: null,
            percentage: '0',
            quantity: '5.0',
            price_usd: null,
            wallet_count: 1,
            read_status: 'ok',
            included: true,
          },
        ],
      })
      expect(container.querySelector('[aria-label="Asset allocation"]')).toBeTruthy()
      expect(container.textContent).not.toMatch(/allocation unavailable/i)
      expect(container.textContent).toContain('unpriced')
      await unmount(container, root)
    })
  })

  describe('news section', () => {
    it('renders the News section wired to priced allocations', async () => {
      const { container, root } = mountWithData({
        ...EMPTY_PORTFOLIO,
        total_usd: '4000.00',
        priced_subtotal_usd: '4000.00',
        allocations: [ETH_ALLOCATION],
      })
      expect(container.querySelector('[aria-label="Asset news"]')).toBeTruthy()
      expect(container.querySelector('[aria-label="News feed stub"]')?.textContent).toBe(
        '1 assets',
      )
      await unmount(container, root)
    })

    it('excludes unpriced assets from the News section item count', async () => {
      const { container, root } = mountWithData({
        ...EMPTY_PORTFOLIO,
        total_usd: '4000.00',
        priced_subtotal_usd: '4000.00',
        allocations: [
          ETH_ALLOCATION,
          makeAllocation({
            asset_id: 'xyz',
            symbol: 'XYZ',
            value_usd: null,
            percentage: '0',
            quantity: '5.0',
            price_usd: null,
          }),
        ],
      })
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

  describe('portfolio value history range switcher', () => {
    const HISTORY_WITH_ENTRIES = {
      period: '30d' as const,
      items: [
        {
          snapshot_id: 'h1',
          snapshotted_at: '2026-01-15T00:00:00Z',
          total_value_usd: '191000.00',
          quality: 'complete' as const,
          included_wallet_count: 1,
          included_asset_count: 1,
          has_gap: false,
          is_canonical: true,
          is_gap_marker: false,
        },
      ],
      next_cursor: null,
      request_id: 'req-1',
      generated_at: '2026-01-15T00:00:00Z',
    }

    async function waitForRangeButtons(container: HTMLDivElement): Promise<HTMLButtonElement[]> {
      let buttons: HTMLButtonElement[] = []
      await vi.waitFor(() => {
        buttons = Array.from(container.querySelectorAll('.range-switcher-btn')) as HTMLButtonElement[]
        expect(buttons.length).toBeGreaterThan(0)
      })
      return buttons
    }

    it('renders four range segments defaulting to 1M', async () => {
      mockFetchHistory.mockResolvedValue(HISTORY_WITH_ENTRIES)
      const { container, root } = mountWithData({
        ...EMPTY_PORTFOLIO,
        total_usd: '4000.00',
        priced_subtotal_usd: '4000.00',
      })
      const buttons = await waitForRangeButtons(container)
      expect(buttons.map((b) => b.textContent)).toEqual(['1D', '1W', '1M', 'All'])
      const active = buttons.find((b) => b.getAttribute('aria-pressed') === 'true')
      expect(active?.textContent).toBe('1M')
      expect(mockFetchHistory).toHaveBeenCalledWith('30d')
      await unmount(container, root)
    })

    it('switching range requests the matching period from the API', async () => {
      mockFetchHistory.mockResolvedValue(HISTORY_WITH_ENTRIES)
      const { container, root } = mountWithData({
        ...EMPTY_PORTFOLIO,
        total_usd: '4000.00',
        priced_subtotal_usd: '4000.00',
      })
      const buttons = await waitForRangeButtons(container)
      const dayButton = buttons.find((b) => b.textContent === '1D')
      expect(dayButton).toBeTruthy()
      await act(async () => {
        dayButton?.click()
      })
      expect(mockFetchHistory).toHaveBeenCalledWith('24h')
      expect(dayButton?.getAttribute('aria-pressed')).toBe('true')
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
            React.createElement(DashboardPage, { setPage: vi.fn() }),
          ),
        )
      })
      expect(container.textContent).toMatch(/loading/i)
      await act(async () => { root.unmount() })
      document.body.removeChild(container)
    })
  })
})
