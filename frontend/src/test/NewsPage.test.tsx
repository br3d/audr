import { describe, it, expect, vi, afterEach } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, fetchPortfolio: vi.fn(), fetchAssetNews: vi.fn() }
})

import NewsPage from '../pages/NewsPage'
import { fetchPortfolio, fetchAssetNews } from '../api/client'

const alloc = (id: string, symbol: string, extra = {}) => ({
  asset_id: id,
  symbol,
  name: symbol,
  value_usd: '10',
  percentage: '50',
  quantity: '1',
  price_usd: '10',
  price_change_24h_pct: null,
  wallet_count: 1,
  read_status: 'ok',
  included: true,
  ...extra,
})

async function mount() {
  const container = document.createElement('div')
  document.body.appendChild(container)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const root = createRoot(container)
  await act(async () => {
    root.render(React.createElement(QueryClientProvider, { client: qc }, React.createElement(NewsPage)))
    await new Promise((r) => setTimeout(r, 0))
  })
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0))
  })
  return container
}

afterEach(() => {
  vi.clearAllMocks()
  document.body.innerHTML = ''
  window.history.replaceState(null, '', '#/news')
})

function setup() {
  vi.mocked(fetchPortfolio).mockResolvedValue({
    allocations: [alloc('eth', 'ETH'), alloc('usdc', 'USDC'), alloc('xyz', 'XYZ', { value_usd: null })],
  } as never)
  vi.mocked(fetchAssetNews).mockResolvedValue({ asset_id: 'x', total: 0, limit: 5, offset: 0, news: [] })
}

describe('NewsPage', () => {
  it('has heading, subheading, and one chip per held priced symbol plus All', async () => {
    setup()
    window.history.replaceState(null, '', '#/news')
    const c = await mount()
    expect(c.querySelector('h2')?.textContent).toBe('News')
    expect(c.textContent).toContain('Headlines for the assets you hold.')
    const chips = Array.from(c.querySelectorAll('[aria-label="Filter by asset"] button')).map(
      (b) => b.textContent,
    )
    expect(chips).toEqual(['All', 'ETH', 'USDC'])
    expect(c.querySelectorAll('[aria-label^="News for"]')).toHaveLength(2)
  })

  it('filters to one asset and keeps it in the hash', async () => {
    setup()
    window.history.replaceState(null, '', '#/news')
    const c = await mount()
    const eth = Array.from(c.querySelectorAll('[aria-label="Filter by asset"] button')).find(
      (b) => b.textContent === 'ETH',
    )!
    await act(async () => eth.dispatchEvent(new MouseEvent('click', { bubbles: true })))
    expect(window.location.hash).toBe('#/news?asset=ETH')
    expect(c.querySelectorAll('[aria-label^="News for"]')).toHaveLength(1)
    expect(c.querySelector('[aria-label="News for ETH"]')).toBeTruthy()
  })

  it('restores the filter from the hash on load', async () => {
    setup()
    window.history.replaceState(null, '', '#/news?asset=USDC')
    const c = await mount()
    expect(c.querySelector('[aria-label="News for USDC"]')).toBeTruthy()
    expect(c.querySelector('[aria-label="News for ETH"]')).toBeNull()
  })

  it('falls back to All for an unknown symbol', async () => {
    setup()
    window.history.replaceState(null, '', '#/news?asset=NOPE')
    const c = await mount()
    expect(c.querySelectorAll('[aria-label^="News for"]')).toHaveLength(2)
  })
})
