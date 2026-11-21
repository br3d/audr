import { useQuery } from '@tanstack/react-query'
import { fetchPortfolio } from '../api/client'
import { queryKeys } from '../api/queryKeys'
import { errorMessage } from '../api/errorMessage'
import NewsFeed from '../components/NewsFeed'
import { stringField, useHashQueryState } from '../routing'

const NEWS_QUERY = { asset: stringField('') }

export default function NewsPage() {
  const [{ asset }, updateQuery] = useHashQueryState('news', NEWS_QUERY)
  const { data, error, isLoading } = useQuery({
    queryKey: queryKeys.portfolio(),
    queryFn: () => fetchPortfolio(),
    refetchInterval: 30_000,
  })

  const held = (data?.allocations ?? []).filter((a) => a.included && a.value_usd !== null)
  // An unknown symbol in the hash falls back to "All" rather than an empty page.
  const active = held.some((a) => a.symbol === asset) ? asset : ''
  const visible = active ? held.filter((a) => a.symbol === active) : held

  return (
    <div>
      <div className="page-header-row">
        <div>
          <div className="page-header-breadcrumb">Portfolio</div>
          <h2 className="page-heading">News</h2>
          <p className="page-subheading">Headlines for the assets you hold.</p>
        </div>
      </div>

      {isLoading && <p aria-busy="true">Loading news…</p>}
      {error && (
        <p role="alert" className="alert alert-danger">
          Failed to load portfolio. {errorMessage(error, 'Please try again.')}
        </p>
      )}

      {data && (
        <>
          {held.length > 0 && (
            <div
              className="range-switcher range-switcher--wrap mb-16"
              role="group"
              aria-label="Filter by asset"
            >
              <button
                type="button"
                className="range-switcher-btn"
                aria-pressed={active === ''}
                onClick={() => updateQuery({ asset: '' })}
              >
                All
              </button>
              {held.map((a) => (
                <button
                  key={a.asset_id}
                  type="button"
                  className="range-switcher-btn"
                  aria-pressed={active === a.symbol}
                  onClick={() => updateQuery({ asset: a.symbol })}
                >
                  {a.symbol}
                </button>
              ))}
            </div>
          )}
          <NewsFeed items={visible} />
        </>
      )}
    </div>
  )
}
