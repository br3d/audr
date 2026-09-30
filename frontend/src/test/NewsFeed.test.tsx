import { describe, it, expect, vi, afterEach } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { AssetNewsResponse } from '../api/client'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return {
    ...actual,
    fetchAssetNews: vi.fn(),
  }
})

import NewsFeed from '../components/NewsFeed'
import { fetchAssetNews, ApiError } from '../api/client'

const mockFetchAssetNews = vi.mocked(fetchAssetNews)

const ETH_ITEM = { asset_id: 'asset-eth', symbol: 'ETH', percentage: '62.50' }
const USDC_ITEM = { asset_id: 'asset-usdc', symbol: 'USDC', percentage: '37.50' }

function makeResponse(overrides: Partial<AssetNewsResponse> = {}): AssetNewsResponse {
  return {
    asset_id: 'asset-eth',
    total: 0,
    limit: 5,
    offset: 0,
    news: [],
    ...overrides,
  }
}

function mount(element: React.ReactElement): { container: HTMLDivElement; root: Root; qc: QueryClient } {
  const container = document.createElement('div')
  document.body.appendChild(container)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const root = createRoot(container)
  act(() => {
    root.render(
      React.createElement(QueryClientProvider, { client: qc }, element),
    )
  })
  return { container, root, qc }
}

async function unmount(container: HTMLDivElement, root: Root) {
  await act(async () => {
    root.unmount()
  })
  document.body.removeChild(container)
}

async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0))
    await new Promise((resolve) => setTimeout(resolve, 0))
  })
}

describe('NewsFeed', () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('shows nothing (no asset cards) when there are no tied assets', async () => {
    const { container, root } = mount(React.createElement(NewsFeed, { items: [] }))
    expect(container.textContent).toMatch(/no assets/i)
    expect(mockFetchAssetNews).not.toHaveBeenCalled()
    await unmount(container, root)
  })

  it('shows a loading indicator for an asset while its news is pending', async () => {
    mockFetchAssetNews.mockReturnValue(new Promise(() => {}))
    const { container, root } = mount(React.createElement(NewsFeed, { items: [ETH_ITEM] }))
    expect(container.textContent).toMatch(/loading/i)
    await unmount(container, root)
  })

  it('shows the empty-cache state (not an error) when total is 0', async () => {
    mockFetchAssetNews.mockResolvedValue(makeResponse({ total: 0, news: [] }))
    const { container, root } = mount(React.createElement(NewsFeed, { items: [ETH_ITEM] }))
    await flush()
    expect(container.textContent).toMatch(/no news cached yet/i)
    expect(container.querySelector('[role="alert"]')).toBeNull()
    await unmount(container, root)
  })

  it('shows the asset symbol and its portfolio percentage', async () => {
    mockFetchAssetNews.mockResolvedValue(makeResponse({ total: 0, news: [] }))
    const { container, root } = mount(React.createElement(NewsFeed, { items: [ETH_ITEM] }))
    await flush()
    expect(container.textContent).toContain('ETH')
    expect(container.textContent).toContain('62.50%')
    await unmount(container, root)
  })

  it('renders news items with title link, source, and date', async () => {
    mockFetchAssetNews.mockResolvedValue(
      makeResponse({
        total: 1,
        news: [
          {
            id: 'n1',
            source: 'coingecko',
            title: 'ETH breaks new ground',
            url: 'https://example.com/eth-news',
            news_site: 'Example Daily',
            thumbnail_url: null,
            published_at: '2026-06-01T00:00:00Z',
            fetched_at: '2026-09-30T00:00:00Z',
          },
        ],
      }),
    )
    const { container, root } = mount(React.createElement(NewsFeed, { items: [ETH_ITEM] }))
    await flush()
    const link = container.querySelector('a[href="https://example.com/eth-news"]')
    expect(link).toBeTruthy()
    expect(link?.textContent).toContain('ETH breaks new ground')
    expect(container.textContent).toContain('Example Daily')
    await unmount(container, root)
  })

  it('shows a per-asset error using the contract envelope message, without failing other assets', async () => {
    mockFetchAssetNews.mockImplementation((assetId: string) => {
      if (assetId === 'asset-eth') {
        return Promise.reject(
          new ApiError(502, {
            error: { code: 'upstream_error', message: 'News provider is unavailable.' },
            request_id: 'req-1',
          }),
        )
      }
      return Promise.resolve(makeResponse({ asset_id: assetId, total: 0, news: [] }))
    })
    const { container, root } = mount(
      React.createElement(NewsFeed, { items: [ETH_ITEM, USDC_ITEM] }),
    )
    await flush()
    expect(container.textContent).toContain('News provider is unavailable.')
    expect(container.textContent).toMatch(/no news cached yet/i)
    await unmount(container, root)
  })

  it('shows a Load more button when more items exist, and fetches the next offset on click', async () => {
    mockFetchAssetNews.mockImplementation((_assetId: string, _limit: number, offset: number) => {
      if (offset === 0) {
        return Promise.resolve(
          makeResponse({
            total: 2,
            offset: 0,
            news: [
              {
                id: 'n1',
                source: 'coingecko',
                title: 'First article',
                url: 'https://example.com/first',
                news_site: 'Example Daily',
                thumbnail_url: null,
                published_at: '2026-06-02T00:00:00Z',
                fetched_at: '2026-09-30T00:00:00Z',
              },
            ],
          }),
        )
      }
      return Promise.resolve(
        makeResponse({
          total: 2,
          offset,
          news: [
            {
              id: 'n2',
              source: 'coingecko',
              title: 'Second article',
              url: 'https://example.com/second',
              news_site: 'Example Daily',
              thumbnail_url: null,
              published_at: '2026-06-01T00:00:00Z',
              fetched_at: '2026-09-30T00:00:00Z',
            },
          ],
        }),
      )
    })
    const { container, root } = mount(React.createElement(NewsFeed, { items: [ETH_ITEM] }))
    await flush()
    expect(container.textContent).toContain('First article')
    expect(container.textContent).not.toContain('Second article')

    const loadMore = Array.from(container.querySelectorAll('button')).find((b) =>
      /load more/i.test(b.textContent ?? ''),
    )
    expect(loadMore).toBeTruthy()

    await act(async () => {
      loadMore?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await flush()

    expect(container.textContent).toContain('Second article')
    const loadMoreAfter = Array.from(container.querySelectorAll('button')).find((b) =>
      /load more/i.test(b.textContent ?? ''),
    )
    expect(loadMoreAfter).toBeFalsy()
    await unmount(container, root)
  })
})
