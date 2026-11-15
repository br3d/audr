import { describe, it, expect, vi, afterEach } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { AlertItem, AllocationItem } from '../api/client'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, fetchAlerts: vi.fn(), fetchAllowances: vi.fn(), fetchAssets: vi.fn(), markAlertRead: vi.fn() }
})

import InFocusPanel from '../components/InFocusPanel'
import { fetchAlerts, fetchAllowances, fetchAssets, markAlertRead } from '../api/client'

const mAlerts = vi.mocked(fetchAlerts)
const mAllow = vi.mocked(fetchAllowances)
const mAssets = vi.mocked(fetchAssets)
const mMark = vi.mocked(markAlertRead)

const alert = (o: Partial<AlertItem>): AlertItem => ({
  id: 'a1',
  asset_id: 'eth',
  asset_news_id: 'n1',
  asset_symbol: 'ETH',
  category: 'hack_exploit',
  classifier: 'x',
  created_at: '2026-11-15T00:00:00Z',
  model: null,
  read_at: null,
  severity: 'critical',
  summary: 'Bridge drained',
  article_title: 'Bridge exploited',
  article_url: 'https://example.com/x',
  ...o,
})
const alertsResp = (alerts: AlertItem[]) => ({ alerts, unread_count: alerts.length, total: alerts.length, limit: 20, offset: 0 }) as never
const allowResp = (n: number) =>
  ({
    total: n,
    limit: 50,
    offset: 0,
    allowances: Array.from({ length: n }, (_, i) => ({
      wallet_id: 'w',
      token_address: `0x${String(i).padStart(40, '1')}`,
      spender_address: '0x1234567890abcdef1234567890abcdef0000abcd',
      raw_amount: '1',
      is_unlimited: true,
      observed_at_block: 1,
      tx_hash: '0x',
      indexed_at: '',
    })),
  }) as never

const move = (o: Partial<AllocationItem> = {}): AllocationItem => ({
  asset_id: 'eth',
  symbol: 'ETH',
  name: 'Ethereum',
  value_usd: '100',
  percentage: '50',
  quantity: '1',
  price_usd: '100',
  price_change_24h_pct: '-12.4',
  wallet_count: 1,
  read_status: 'ok',
  included: true,
  ...o,
})

async function mount(allocs: AllocationItem[], setPage = vi.fn()) {
  const container = document.createElement('div')
  document.body.appendChild(container)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const root = createRoot(container)
  await act(async () => {
    root.render(
      React.createElement(QueryClientProvider, { client: qc }, React.createElement(InFocusPanel, { allocations: allocs, setPage })),
    )
    await new Promise((r) => setTimeout(r, 0))
  })
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0))
  })
  return { container, root, setPage, qc }
}

afterEach(() => {
  vi.clearAllMocks()
  document.body.innerHTML = ''
})

describe('InFocusPanel', () => {
  it('shows the honest empty state, titled "In focus" (not "AI")', async () => {
    mAlerts.mockResolvedValue(alertsResp([]))
    mAllow.mockResolvedValue(allowResp(0))
    const { container } = await mount([move({ price_change_24h_pct: '3' })])
    expect(container.textContent).toContain('In focus')
    expect(container.textContent).toContain('Nothing needs your attention right now.')
    expect(container.textContent).not.toMatch(/\bAI\b/)
  })

  it('orders signals, caps at 4, shows count and +N more linking to Events', async () => {
    mAlerts.mockResolvedValue(
      alertsResp([alert({ id: 'w', severity: 'warning', article_title: 'Warn title' }), alert({ id: 'c' })]),
    )
    mAllow.mockResolvedValue(allowResp(2))
    mAssets.mockResolvedValue({ items: [] } as never)
    const { container, setPage } = await mount([move()])
    expect(container.textContent).toContain('5 signals')
    const items = Array.from(container.querySelectorAll('li.infocus-item'))
    expect(items).toHaveLength(4)
    expect(items[0].textContent).toContain('Bridge exploited')
    expect(items[0].querySelector('.pill.risk')).toBeTruthy()
    expect(items[1].querySelector('.pill.warn')).toBeTruthy()
    expect(items[2].textContent).toContain('ETH −12.4% in 24h · 50% of portfolio')
    expect(items[3].textContent).toContain('Unlimited allowance')
    const more = Array.from(container.querySelectorAll('button')).find((b) => b.textContent === '+1 more')!
    act(() => more.click())
    expect(setPage).toHaveBeenCalledWith('events')
    const link = container.querySelector('a[href="https://example.com/x"]')!
    expect(link.getAttribute('target')).toBe('_blank')
  })

  it('a failing source removes only its own signals', async () => {
    mAlerts.mockRejectedValue(new Error('boom'))
    mAllow.mockResolvedValue(allowResp(1))
    mAssets.mockResolvedValue({ items: [] } as never)
    const { container } = await mount([move()])
    expect(container.textContent).toContain('−12.4% in 24h')
    expect(container.textContent).toContain('Unlimited allowance')
    expect(container.textContent).toContain('Couldn’t check alerts')
    expect(container.textContent).not.toContain('Nothing needs your attention')
  })

  it('marks an alert read and refreshes the shared alerts key', async () => {
    mAlerts.mockResolvedValue(alertsResp([alert({})]))
    mAllow.mockResolvedValue(allowResp(0))
    mMark.mockResolvedValue({ id: 'a1', read_at: 'x' })
    const { container, qc } = await mount([])
    const spy = vi.spyOn(qc, 'invalidateQueries')
    const btn = Array.from(container.querySelectorAll('button')).find((b) => b.textContent === 'Mark read')!
    await act(async () => {
      btn.click()
      await new Promise((r) => setTimeout(r, 0))
    })
    expect(mMark).toHaveBeenCalledWith('a1')
    expect(spy).toHaveBeenCalledWith({ queryKey: ['alerts'] })
  })
})
