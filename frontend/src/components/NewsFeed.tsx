import { useInfiniteQuery } from '@tanstack/react-query'
import { fetchAssetNews, ApiError } from '../api/client'

const PAGE_SIZE = 5

export interface NewsFeedAsset {
  asset_id: string
  symbol: string
  percentage: string
}

interface AssetNewsCardProps {
  asset: NewsFeedAsset
}

function formatPublishedAt(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' })
}

function AssetNewsCard({ asset }: AssetNewsCardProps) {
  const { data, error, isLoading, hasNextPage, fetchNextPage, isFetchingNextPage } =
    useInfiniteQuery({
      queryKey: ['asset-news', asset.asset_id],
      queryFn: ({ pageParam }) => fetchAssetNews(asset.asset_id, PAGE_SIZE, pageParam as number),
      initialPageParam: 0,
      getNextPageParam: (lastPage, allPages) => {
        const loaded = allPages.reduce((sum, p) => sum + p.news.length, 0)
        return loaded < lastPage.total ? loaded : undefined
      },
    })

  const items = data?.pages.flatMap((p) => p.news) ?? []
  const total = data?.pages[0]?.total ?? null

  return (
    <div className="card mb-16" aria-label={`News for ${asset.symbol}`}>
      <div className="card-header">
        <div className="card-title">{asset.symbol}</div>
        <span className="badge badge-neutral" aria-label={`${asset.percentage} percent of portfolio`}>
          {asset.percentage}%
        </span>
      </div>

      {isLoading && <p aria-busy="true">Loading news…</p>}

      {error && (
        <p role="alert" className="alert alert-danger">
          {error instanceof ApiError ? error.message : 'Failed to load news.'}
        </p>
      )}

      {!isLoading && !error && total === 0 && (
        <p role="note" className="text-muted">
          No news cached yet for {asset.symbol}.
        </p>
      )}

      {items.length > 0 && (
        <ul role="list" className="news-list">
          {items.map((n) => (
            <li key={n.id} className="news-item">
              <a href={n.url} target="_blank" rel="noopener noreferrer" className="news-item-title">
                {n.title}
              </a>
              <div className="news-item-meta text-secondary">
                {n.news_site} · {formatPublishedAt(n.published_at)}
              </div>
            </li>
          ))}
        </ul>
      )}

      {hasNextPage && (
        <button
          type="button"
          className="btn btn-secondary btn-sm mt-8"
          onClick={() => void fetchNextPage()}
          disabled={isFetchingNextPage}
        >
          {isFetchingNextPage ? 'Loading…' : 'Load more'}
        </button>
      )}
    </div>
  )
}

interface NewsFeedProps {
  items: NewsFeedAsset[]
}

export default function NewsFeed({ items }: NewsFeedProps) {
  if (items.length === 0) {
    return (
      <p role="note" className="text-muted">
        No assets with a known portfolio share to show news for yet.
      </p>
    )
  }

  return (
    <div>
      {items.map((asset) => (
        <AssetNewsCard key={asset.asset_id} asset={asset} />
      ))}
    </div>
  )
}
