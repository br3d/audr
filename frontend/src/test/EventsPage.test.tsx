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
    fetchEvents: vi.fn(),
    fetchAllowances: vi.fn(),
    fetchWallets: vi.fn(),
    ApiError,
  }
})

import EventsPage from '../pages/EventsPage'
import { fetchEvents, fetchAllowances, fetchWallets } from '../api/client'
import type { EventsResponse, AllowancesResponse, OnchainEvent, Allowance, WalletItem } from '../api/client'

const mockFetchEvents = vi.mocked(fetchEvents)
const mockFetchAllowances = vi.mocked(fetchAllowances)
const mockFetchWallets = vi.mocked(fetchWallets)

const WALLET_1: WalletItem = {
  id: 'w1',
  address: '0xABCDEF1234567890ABCDef1234567890abcdef12',
  label: 'My Cold Wallet',
  chain_id: 1,
  tracking_active: true,
  coverage: null,
  created_at: '2026-01-01T00:00:00Z',
}

const EVENT_1: OnchainEvent = {
  id: 'e1',
  wallet_id: 'w1',
  tx_hash: '0x1111111111111111111111111111111111111111111111111111111111111111',
  block_number: 100,
  log_index: 0,
  event_type: 'transfer_in',
  token_address: '0x2222222222222222222222222222222222222222',
  from_address: '0x3333333333333333333333333333333333333333',
  to_address: '0xabcdef1234567890abcdef1234567890abcdef12',
  raw_amount: '1000000000000000000',
  indexed_at: '2026-06-01T00:00:00Z',
}

const ALLOWANCE_LIMITED: Allowance = {
  wallet_id: 'w1',
  token_address: '0x2222222222222222222222222222222222222222',
  spender_address: '0x4444444444444444444444444444444444444444',
  raw_amount: '500000000000000000',
  is_unlimited: false,
  observed_at_block: 90,
  tx_hash: '0x5555555555555555555555555555555555555555555555555555555555555555',
  indexed_at: '2026-06-01T00:00:00Z',
}

const ALLOWANCE_UNLIMITED: Allowance = {
  wallet_id: 'w1',
  token_address: '0x2222222222222222222222222222222222222222',
  spender_address: '0x6666666666666666666666666666666666666666',
  raw_amount: '340282366920938463463374607431768211455',
  is_unlimited: true,
  observed_at_block: 95,
  tx_hash: '0x7777777777777777777777777777777777777777777777777777777777777777',
  indexed_at: '2026-06-02T00:00:00Z',
}

function makeEventsResponse(overrides: Partial<EventsResponse> = {}): EventsResponse {
  return { total: 0, limit: 50, offset: 0, events: [], ...overrides }
}

function makeAllowancesResponse(overrides: Partial<AllowancesResponse> = {}): AllowancesResponse {
  return { total: 0, limit: 50, offset: 0, allowances: [], ...overrides }
}

function makeWalletsResponse(items: WalletItem[] = [WALLET_1]) {
  return { items, next_cursor: null, request_id: 'r1', generated_at: '2026-01-01T00:00:00Z' }
}

function mountPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  act(() => {
    root.render(
      React.createElement(QueryClientProvider, { client: qc }, React.createElement(EventsPage)),
    )
  })
  return { container, root }
}

describe('EventsPage', () => {
  let container: HTMLDivElement
  let root: Root

  afterEach(async () => {
    await act(async () => {
      root.unmount()
    })
    document.body.removeChild(container)
    vi.clearAllMocks()
  })

  it('renders both the activity and permissions tables on real API responses', async () => {
    mockFetchWallets.mockResolvedValue(makeWalletsResponse())
    mockFetchEvents.mockResolvedValue(makeEventsResponse({ total: 1, events: [EVENT_1] }))
    mockFetchAllowances.mockResolvedValue(
      makeAllowancesResponse({ total: 1, allowances: [ALLOWANCE_LIMITED] }),
    )
    ;({ container, root } = mountPage())

    await vi.waitFor(() => {
      expect(container.querySelector('table[aria-label="On-chain events"]')).toBeTruthy()
      expect(container.querySelector('table[aria-label="Token allowances"]')).toBeTruthy()
    })
    expect(container.textContent).toContain('My Cold Wallet')
  })

  it('shows an honest empty state for events instead of a spinner', async () => {
    mockFetchWallets.mockResolvedValue(makeWalletsResponse())
    mockFetchEvents.mockResolvedValue(makeEventsResponse())
    mockFetchAllowances.mockResolvedValue(makeAllowancesResponse())
    ;({ container, root } = mountPage())

    await vi.waitFor(() => expect(container.textContent).toMatch(/no events found/i))
    expect(container.querySelector('[aria-busy="true"]')).toBeNull()
  })

  it('shows an honest empty state for allowances', async () => {
    mockFetchWallets.mockResolvedValue(makeWalletsResponse())
    mockFetchEvents.mockResolvedValue(makeEventsResponse())
    mockFetchAllowances.mockResolvedValue(makeAllowancesResponse())
    ;({ container, root } = mountPage())

    await vi.waitFor(() => expect(container.textContent).toMatch(/no allowances found/i))
  })

  it('refetches events with the selected wallet_id filter', async () => {
    mockFetchWallets.mockResolvedValue(makeWalletsResponse())
    mockFetchEvents.mockResolvedValue(makeEventsResponse({ total: 1, events: [EVENT_1] }))
    mockFetchAllowances.mockResolvedValue(makeAllowancesResponse())
    ;({ container, root } = mountPage())

    await vi.waitFor(() => expect(mockFetchEvents).toHaveBeenCalled())

    await vi.waitFor(() =>
      expect(container.querySelector('#events-wallet-filter option[value="w1"]')).toBeTruthy(),
    )
    const walletSelect = container.querySelector('#events-wallet-filter') as HTMLSelectElement

    await act(async () => {
      walletSelect.value = 'w1'
      walletSelect.dispatchEvent(new Event('change', { bubbles: true }))
    })

    await vi.waitFor(() => {
      expect(mockFetchEvents).toHaveBeenCalledWith(
        expect.objectContaining({ walletId: 'w1' }),
      )
      expect(mockFetchAllowances).toHaveBeenCalledWith(
        expect.objectContaining({ walletId: 'w1' }),
      )
    })
  })

  it('refetches events with the selected event_type filter', async () => {
    mockFetchWallets.mockResolvedValue(makeWalletsResponse())
    mockFetchEvents.mockResolvedValue(makeEventsResponse({ total: 1, events: [EVENT_1] }))
    mockFetchAllowances.mockResolvedValue(makeAllowancesResponse())
    ;({ container, root } = mountPage())

    await vi.waitFor(() => expect(mockFetchEvents).toHaveBeenCalled())

    const typeSelect = container.querySelector('#events-type-filter') as HTMLSelectElement
    expect(typeSelect).toBeTruthy()

    await act(async () => {
      typeSelect.value = 'transfer_out'
      typeSelect.dispatchEvent(new Event('change', { bubbles: true }))
    })

    await vi.waitFor(() => {
      expect(mockFetchEvents).toHaveBeenCalledWith(
        expect.objectContaining({ eventType: 'transfer_out' }),
      )
    })
  })

  it('refetches allowances with unlimited_only when the toggle is checked', async () => {
    mockFetchWallets.mockResolvedValue(makeWalletsResponse())
    mockFetchEvents.mockResolvedValue(makeEventsResponse())
    mockFetchAllowances.mockResolvedValue(makeAllowancesResponse())
    ;({ container, root } = mountPage())

    await vi.waitFor(() => expect(mockFetchAllowances).toHaveBeenCalled())

    const checkbox = Array.from(container.querySelectorAll('input[type="checkbox"]')).find((c) =>
      /unlimited/i.test(c.closest('label')?.textContent ?? ''),
    ) as HTMLInputElement
    expect(checkbox).toBeTruthy()

    await act(async () => {
      checkbox.click()
    })

    await vi.waitFor(() => {
      expect(mockFetchAllowances).toHaveBeenCalledWith(
        expect.objectContaining({ unlimitedOnly: true }),
      )
    })
  })

  it('paginates events via limit/offset using Previous/Next', async () => {
    mockFetchWallets.mockResolvedValue(makeWalletsResponse())
    mockFetchEvents.mockResolvedValue(makeEventsResponse({ total: 120, limit: 50, offset: 0, events: [EVENT_1] }))
    mockFetchAllowances.mockResolvedValue(makeAllowancesResponse())
    ;({ container, root } = mountPage())

    await vi.waitFor(() => expect(container.textContent).toContain('1–50 of 120'))

    const nextBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent === 'Next',
    ) as HTMLButtonElement
    expect(nextBtn.disabled).toBe(false)

    await act(async () => {
      nextBtn.click()
    })

    await vi.waitFor(() => {
      expect(mockFetchEvents).toHaveBeenCalledWith(expect.objectContaining({ offset: 50 }))
    })
  })

  it('marks is_unlimited allowances as a visible risk signal', async () => {
    mockFetchWallets.mockResolvedValue(makeWalletsResponse())
    mockFetchEvents.mockResolvedValue(makeEventsResponse())
    mockFetchAllowances.mockResolvedValue(
      makeAllowancesResponse({
        total: 2,
        allowances: [ALLOWANCE_LIMITED, ALLOWANCE_UNLIMITED],
      }),
    )
    ;({ container, root } = mountPage())

    await vi.waitFor(() =>
      expect(container.querySelector('table[aria-label="Token allowances"] tbody tr')).toBeTruthy(),
    )

    const riskyRow = container.querySelector('tr.row-risk')
    expect(riskyRow).toBeTruthy()
    expect(riskyRow?.querySelector('.badge-danger')).toBeTruthy()

    const safeRows = Array.from(container.querySelectorAll('table[aria-label="Token allowances"] tbody tr')).filter(
      (r) => !r.classList.contains('row-risk'),
    )
    expect(safeRows.length).toBe(1)
  })

  it('never passes raw_amount through Number() — exact large values render intact', async () => {
    mockFetchWallets.mockResolvedValue(makeWalletsResponse())
    mockFetchEvents.mockResolvedValue(makeEventsResponse())
    mockFetchAllowances.mockResolvedValue(
      makeAllowancesResponse({ total: 1, allowances: [ALLOWANCE_UNLIMITED] }),
    )
    ;({ container, root } = mountPage())

    // type(uint256).max-scale value — if this were ever coerced through
    // Number(), it would lose precision and no longer match the exact digits.
    await vi.waitFor(() =>
      expect(container.textContent).toContain('340,282,366,920,938,463,463,374,607,431,768,211,455'),
    )
  })
})
