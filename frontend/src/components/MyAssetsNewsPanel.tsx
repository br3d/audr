import { useQueries } from '@tanstack/react-query'
import { fetchAssetNews } from '../api/client'
import type { AssetNewsItem } from '../api/client'
import { queryKeys } from '../api/queryKeys'
import type { MainPage } from './Layout'
import type { NewsFeedAsset } from './NewsFeed'

const COMPACT_NEWS_LIMIT = 3

export function relativeTime(iso: string, now: number = Date.now()): string {
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return iso
  const mins = Math.max(0, Math.round((now - t) / 60_000))
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins} min ago`
  const hours = Math.round(mins / 60)
  if (hours < 24) return `${hours} h ago`
  const days = Math.round(hours / 24)
  if (days < 30) return `${days} d ago`
  return new Date(iso).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' })
}

interface Props {
  assets: NewsFeedAsset[]
  setPage: (page: MainPage) => void
}

/**
 * Compact cross-asset headline list (AUD-947). There is no cross-asset news
 * endpoint, so this fetches the newest few per asset and merges client-side;
 * replace with a single call if the API grows one.
 */
export default function MyAssetsNewsPanel({ assets, setPage }: Props) {
  const results = useQueries({
    queries: assets.map((a) => ({
      queryKey: queryKeys.assetNewsLatest(a.asset_id),
      queryFn: () => fetchAssetNews(a.asset_id, COMPACT_NEWS_LIMIT, 0),
      staleTime: 60_000,
    })),
  })

  const merged: { symbol: string; item: AssetNewsItem }[] = []
  results.forEach((r, i) => {
    for (const item of r.data?.news ?? []) merged.push({ symbol: assets[i].symbol, item })
  })
  merged.sort((a, b) => b.item.published_at.localeCompare(a.item.published_at))
  const top = merged.slice(0, COMPACT_NEWS_LIMIT)
  const loading = results.length > 0 && results.every((r) => r.isLoading)

  return (
    <section aria-label="My assets news" className="card mb-20">
      <div className="card-header">
        <div className="card-title">My assets news</div>
        <button type="button" className="link-button" onClick={() => setPage('news')}>
          All ↗
        </button>
      </div>
      {loading ? (
        <p aria-busy="true">Loading news…</p>
      ) : top.length === 0 ? (
        <p role="note" className="muted-text">
          No news cached yet for your assets.
        </p>
      ) : (
        <ul role="list" className="news-list">
          {top.map(({ symbol, item }) => (
            <li key={item.id} className="news-item">
              <div className="infocus-head">
                <span className="pill">{symbol}</span>
                <span className="text-secondary">{relativeTime(item.published_at)}</span>
              </div>
              <a href={item.url} target="_blank" rel="noopener noreferrer" className="news-item-title">
                {item.title}
              </a>
              <div className="news-item-meta text-secondary">{item.news_site}</div>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
