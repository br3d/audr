import { describe, it, expect, vi, afterEach } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { AssetNewsItem } from '../api/client'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, fetchAssetNews: vi.fn() }
})

import MyAssetsNewsPanel, { relativeTime } from '../components/MyAssetsNewsPanel'
import { fetchAssetNews } from '../api/client'

const mNews = vi.mocked(fetchAssetNews)
const item = (id: string, at: string): AssetNewsItem => ({
  id,
  source: 's',
  title: `Title ${id}`,
  url: `https://example.com/${id}`,
  news_site: 'Site',
  thumbnail_url: null,
  published_at: at,
  fetched_at: at,
})
const resp = (news: AssetNewsItem[]) =>
  ({ asset_id: 'x', total: news.length, limit: 3, offset: 0, news }) as never

async function mount(assets: { asset_id: string; symbol: string; percentage: string }[], setPage = vi.fn()) {
  const container = document.createElement('div')
  document.body.appendChild(container)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const root = createRoot(container)
  await act(async () => {
    root.render(
      React.createElement(
        QueryClientProvider,
        { client: qc },
        React.createElement(MyAssetsNewsPanel, { assets, setPage }),
      ),
    )
    await new Promise((r) => setTimeout(r, 0))
  })
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0))
  })
  return { container, setPage }
}

afterEach(() => {
  vi.clearAllMocks()
  document.body.innerHTML = ''
})

describe('MyAssetsNewsPanel', () => {
  it('merges the newest 3 across assets, newest first, with asset pill and source', async () => {
    mNews.mockImplementation(async (id) =>
      id === 'eth'
        ? resp([
            item('e1', '2026-11-15T10:00:00Z'),
            item('e2', '2026-11-15T08:00:00Z'),
            item('e3', '2026-11-14T00:00:00Z'),
          ])
        : resp([item('u1', '2026-11-15T09:00:00Z'), item('u2', '2026-11-15T07:00:00Z')]),
    )
    const { container } = await mount([
      { asset_id: 'eth', symbol: 'ETH', percentage: '60' },
      { asset_id: 'usdc', symbol: 'USDC', percentage: '40' },
    ])
    const rows = Array.from(container.querySelectorAll('li.news-item'))
    expect(rows.map((r) => r.querySelector('a')?.textContent)).toEqual(['Title e1', 'Title u1', 'Title e2'])
    expect(rows[1].querySelector('.pill')?.textContent).toBe('USDC')
    expect(rows[0].textContent).toContain('Site')
    expect(rows[0].querySelector('a')?.getAttribute('target')).toBe('_blank')
  })

  it('links "All ↗" to the News page', async () => {
    mNews.mockResolvedValue(resp([]))
    const { container, setPage } = await mount([{ asset_id: 'eth', symbol: 'ETH', percentage: '100' }])
    const all = Array.from(container.querySelectorAll('button')).find((b) => b.textContent === 'All ↗')!
    act(() => all.click())
    expect(setPage).toHaveBeenCalledWith('news')
  })

  it('shows the empty state when nothing is cached', async () => {
    mNews.mockResolvedValue(resp([]))
    const { container } = await mount([{ asset_id: 'eth', symbol: 'ETH', percentage: '100' }])
    expect(container.textContent).toContain('No news cached yet for your assets.')
  })
})

describe('relativeTime', () => {
  it('formats minutes, hours and days', () => {
    const now = Date.parse('2026-11-15T12:00:00Z')
    expect(relativeTime('2026-11-15T11:25:00Z', now)).toBe('35 min ago')
    expect(relativeTime('2026-11-15T09:00:00Z', now)).toBe('3 h ago')
    expect(relativeTime('2026-11-13T12:00:00Z', now)).toBe('2 d ago')
  })
})
