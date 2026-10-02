import { useState } from 'react'
import { Decimal } from 'decimal.js'
import type { AllocationItem } from '../api/client'
import AssetEmblem from './AssetEmblem'
import MoneyValue from './MoneyValue'

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

function sum(items: AllocationItem[], key: 'value_usd' | 'percentage'): string {
  return items
    .reduce((acc, i) => acc.plus(new Decimal(i[key])), new Decimal(0))
    .toFixed(2)
}

function AllocationRow({ item }: { item: AllocationItem }) {
  // parseFloat is acceptable here: it only sizes the decorative share bar.
  const barWidth = Math.max(parseFloat(item.percentage), 1.5)

  return (
    <tr>
      <td>
        <div className="allocation-asset">
          <AssetEmblem symbol={item.symbol} logoUrl={item.logo_url} />
          <span className="allocation-symbol">{item.symbol}</span>
        </div>
      </td>
      <td className="td-mono allocation-value">
        <MoneyValue value={item.value_usd} />
      </td>
      <td className="allocation-share">
        <div className="allocation-share-cell">
          <span className="allocation-bar" aria-hidden="true">
            <span className="allocation-bar-fill" style={{ width: `${barWidth}%` }} />
          </span>
          <span className="allocation-pct" aria-label={`${item.percentage} percent`}>
            {item.percentage}%
          </span>
        </div>
      </td>
    </tr>
  )
}

export default function AllocationList({ items }: Props) {
  const [expanded, setExpanded] = useState(false)

  if (items.length === 0) {
    return <p role="note">No allocation data available.</p>
  }

  const { visible, hidden } = splitAllocations(items)
  const rows = expanded ? [...visible, ...hidden] : visible

  return (
    <div className="allocation-list">
      <div className="table-container">
        <table className="table-folio" aria-label="Asset allocation">
          <thead>
            <tr>
              <th scope="col">Asset</th>
              <th scope="col">Value (USD)</th>
              <th scope="col">Allocation</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((item) => (
              <AllocationRow key={item.asset_id} item={item} />
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
    </div>
  )
}
