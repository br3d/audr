import { useState } from 'react'
import { Decimal } from 'decimal.js'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchAssets, patchAsset, ApiError } from '../api/client'
import type { AllocationItem, ReadStatus } from '../api/client'
import AssetEmblem from './AssetEmblem'
import MoneyValue from './MoneyValue'
import { IconSearch } from './Icons'

interface Props {
  items: AllocationItem[]
}

/** Shares at or above this percentage are always considered significant. */
const SIGNIFICANT_SHARE_PCT = 1
/** Always show at least this many rows, even when everything is dust. */
const MIN_VISIBLE = 5
/** Never show more than this many rows collapsed — the rest goes under the spoiler. */
const MAX_VISIBLE = 12
/** Collapsing fewer rows than this is not worth a spoiler; show them inline instead. */
const MIN_COLLAPSED = 3

function clamp(value: number, lo: number, hi: number): number {
  return Math.min(Math.max(value, lo), hi)
}

/**
 * Split the holdings into the ones worth showing up front and the long tail of dust.
 * The split keeps the collapsed view readable: significant shares first, bounded by a
 * floor so a flat portfolio still shows something, and a ceiling so a 200-asset wallet
 * does not flood the card.
 */
export function splitAllocations(items: AllocationItem[]): {
  visible: AllocationItem[]
  hidden: AllocationItem[]
} {
  const sorted = [...items].sort((a, b) =>
    new Decimal(b.percentage).comparedTo(new Decimal(a.percentage)),
  )
  const significant = sorted.filter((i) =>
    new Decimal(i.percentage).greaterThanOrEqualTo(SIGNIFICANT_SHARE_PCT),
  ).length
  const cut = clamp(significant, MIN_VISIBLE, MAX_VISIBLE)

  if (sorted.length - cut < MIN_COLLAPSED) {
    return { visible: sorted, hidden: [] }
  }
  return { visible: sorted.slice(0, cut), hidden: sorted.slice(cut) }
}

/** Unpriced rows carry no USD contribution; the spoiler summary treats them as zero. */
function sum(items: AllocationItem[], key: 'value_usd' | 'percentage'): string {
  return items
    .reduce((acc, i) => acc.plus(i[key] !== null ? new Decimal(i[key]!) : 0), new Decimal(0))
    .toFixed(2)
}

function readBadge(status: ReadStatus) {
  switch (status) {
    case 'ok':
      return <span className="badge badge-ok">Current</span>
    case 'stale':
      return <span className="badge badge-stale">Stale</span>
    case 'error':
      return <span className="badge badge-error">Error</span>
    case 'pending':
      return <span className="badge badge-neutral">Pending</span>
  }
}

function AllocationRow({
  item,
  pendingExclude,
  excluding,
  error,
  onExclude,
}: {
  item: AllocationItem
  pendingExclude: boolean
  excluding: boolean
  error: string | null
  onExclude: () => void
}) {
  const isUnpriced = item.value_usd === null
  // parseFloat is acceptable here: it only sizes the decorative share bar.
  const barWidth = Math.max(parseFloat(item.percentage), 1.5)
  const showExcludedBadge = !item.included || pendingExclude

  return (
    <tr className={pendingExclude ? 'allocation-row-pending' : undefined}>
      <td>
        <div className="allocation-asset">
          <AssetEmblem symbol={item.symbol} logoUrl={item.logo_url} />
          <span className="allocation-symbol">{item.symbol}</span>
          {showExcludedBadge && (
            <span className="badge badge-neutral" aria-label="Excluded from total">
              excluded
            </span>
          )}
          <span aria-label={`Read status: ${item.read_status}`}>{readBadge(item.read_status)}</span>
        </div>
      </td>
      <td className="td-mono allocation-quantity">
        {item.quantity !== null ? item.quantity : <span className="text-muted">unknown</span>}
      </td>
      <td className="td-mono allocation-value">
        {isUnpriced ? (
          <span className="text-muted">unpriced</span>
        ) : (
          <MoneyValue value={item.value_usd} />
        )}
      </td>
      <td className="allocation-share">
        {isUnpriced ? (
          <span className="text-muted">—</span>
        ) : (
          <div className="allocation-share-cell">
            <span className="allocation-bar" aria-hidden="true">
              <span className="allocation-bar-fill" style={{ width: `${barWidth}%` }} />
            </span>
            <span className="allocation-pct" aria-label={`${item.percentage} percent`}>
              {item.percentage}%
            </span>
          </div>
        )}
      </td>
      <td className="allocation-actions">
        {pendingExclude ? (
          <span className="text-muted allocation-pending-note">Applies next snapshot</span>
        ) : (
          <button
            type="button"
            className="btn btn-sm btn-ghost"
            onClick={onExclude}
            disabled={excluding}
            aria-label={`Exclude ${item.symbol}`}
          >
            {excluding ? 'Excluding…' : 'Exclude'}
          </button>
        )}
        {error !== null && (
          <p role="alert" className="text-danger allocation-row-error">
            {error}
          </p>
        )}
      </td>
    </tr>
  )
}

/**
 * Collapsed strip of currently-excluded assets, with an Include button to undo —
 * the only way back once a row has been excluded from the allocation table above,
 * since an excluded asset drops out of the allocations response entirely.
 */
function ExcludedAssetsStrip() {
  const [expanded, setExpanded] = useState(false)
  const [includingIds, setIncludingIds] = useState<Set<string>>(new Set())
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({})
  const queryClient = useQueryClient()

  const { data } = useQuery({
    queryKey: ['assets', 'excluded-strip'],
    queryFn: () => fetchAssets({ excluded: true, held: true }),
  })

  const excludedAssets = data?.items ?? []

  async function handleInclude(assetId: string) {
    setIncludingIds((prev) => new Set(prev).add(assetId))
    setRowErrors((prev) => {
      const next = { ...prev }
      delete next[assetId]
      return next
    })
    try {
      await patchAsset(assetId, { excluded: false })
      void queryClient.invalidateQueries({ queryKey: ['portfolio'] })
      void queryClient.invalidateQueries({ queryKey: ['assets'] })
    } catch (err) {
      setRowErrors((prev) => ({
        ...prev,
        [assetId]: err instanceof ApiError ? err.message : 'Failed to include.',
      }))
    } finally {
      setIncludingIds((prev) => {
        const next = new Set(prev)
        next.delete(assetId)
        return next
      })
    }
  }

  if (excludedAssets.length === 0) return null

  return (
    <div className="allocation-excluded-strip">
      <button
        type="button"
        className="allocation-spoiler"
        aria-expanded={expanded}
        onClick={() => setExpanded((v) => !v)}
      >
        <span className="allocation-spoiler-chevron" aria-hidden="true">
          {expanded ? '▾' : '▸'}
        </span>
        <span className="allocation-spoiler-label">Excluded ({excludedAssets.length})</span>
      </button>

      {expanded && (
        <ul className="allocation-excluded-list" role="list" aria-label="Excluded assets">
          {excludedAssets.map((asset) => (
            <li key={asset.id} className="allocation-excluded-row">
              <span className="allocation-excluded-symbol">{asset.symbol}</span>
              <button
                type="button"
                className="btn btn-sm btn-ghost"
                onClick={() => void handleInclude(asset.id)}
                disabled={includingIds.has(asset.id)}
                aria-label={`Include ${asset.symbol}`}
              >
                {includingIds.has(asset.id) ? 'Including…' : 'Include'}
              </button>
              {rowErrors[asset.id] !== undefined && (
                <p role="alert" className="text-danger allocation-row-error">
                  {rowErrors[asset.id]}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export default function AllocationList({ items }: Props) {
  const [expanded, setExpanded] = useState(false)
  const [search, setSearch] = useState('')
  const [pendingExcludedIds, setPendingExcludedIds] = useState<Set<string>>(new Set())
  const [excludingIds, setExcludingIds] = useState<Set<string>>(new Set())
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({})
  const queryClient = useQueryClient()

  async function handleExclude(assetId: string) {
    setExcludingIds((prev) => new Set(prev).add(assetId))
    setRowErrors((prev) => {
      const next = { ...prev }
      delete next[assetId]
      return next
    })
    try {
      await patchAsset(assetId, { excluded: true })
      setPendingExcludedIds((prev) => new Set(prev).add(assetId))
      void queryClient.invalidateQueries({ queryKey: ['portfolio'] })
      void queryClient.invalidateQueries({ queryKey: ['assets'] })
    } catch (err) {
      setRowErrors((prev) => ({
        ...prev,
        [assetId]: err instanceof ApiError ? err.message : 'Failed to exclude.',
      }))
    } finally {
      setExcludingIds((prev) => {
        const next = new Set(prev)
        next.delete(assetId)
        return next
      })
    }
  }

  if (items.length === 0) {
    return (
      <div className="allocation-list">
        <p role="note">No allocation data available.</p>
        <ExcludedAssetsStrip />
      </div>
    )
  }

  const filtered = search.trim()
    ? items.filter((i) => i.symbol.toLowerCase().includes(search.trim().toLowerCase()))
    : items

  const { visible, hidden } = splitAllocations(filtered)
  const rows = expanded ? [...visible, ...hidden] : visible

  return (
    <div className="allocation-list">
      <div className="toolbar">
        <div className="search-bar">
          <IconSearch className="search-icon" />
          <input
            type="search"
            className="search-input"
            placeholder="Search by symbol…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label="Filter allocations"
          />
        </div>
        <span className="muted-text">
          {filtered.length} of {items.length}
        </span>
      </div>

      <div className="table-container">
        <table className="table-folio" aria-label="Asset allocation">
          <thead>
            <tr>
              <th scope="col">Asset</th>
              <th scope="col">Amount</th>
              <th scope="col">Value (USD)</th>
              <th scope="col">Allocation</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((item) => (
              <AllocationRow
                key={item.asset_id}
                item={item}
                pendingExclude={pendingExcludedIds.has(item.asset_id)}
                excluding={excludingIds.has(item.asset_id)}
                error={rowErrors[item.asset_id] ?? null}
                onExclude={() => void handleExclude(item.asset_id)}
              />
            ))}
          </tbody>
        </table>
      </div>

      {hidden.length > 0 && (
        <button
          type="button"
          className="allocation-spoiler"
          aria-expanded={expanded}
          onClick={() => setExpanded((v) => !v)}
        >
          <span className="allocation-spoiler-chevron" aria-hidden="true">
            {expanded ? '▾' : '▸'}
          </span>
          <span className="allocation-spoiler-label">
            {expanded ? 'Hide' : 'Show'} {hidden.length} smaller{' '}
            {hidden.length === 1 ? 'asset' : 'assets'}
          </span>
          <span className="allocation-spoiler-meta">
            <MoneyValue value={sum(hidden, 'value_usd')} />
            <span className="allocation-spoiler-pct">{sum(hidden, 'percentage')}%</span>
          </span>
        </button>
      )}

      <ExcludedAssetsStrip />
    </div>
  )
}
