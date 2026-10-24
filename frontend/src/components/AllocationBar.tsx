import { Decimal } from 'decimal.js'
import type { AllocationItem } from '../api/client'
import { emblemColor } from './AssetEmblem'

interface Props {
  /** Included, priced allocations only — the same set the Allocation column sums to 100%. */
  items: AllocationItem[]
}

/**
 * Segments below this share collapse into a single "Other" bucket. Without a floor,
 * a wallet holding a couple hundred dust tokens turns the bar into unreadable slivers.
 */
export const OTHER_THRESHOLD_PCT = 1

export interface AllocationSegment {
  key: string
  symbol: string
  pct: Decimal
  color: string
}

export function buildSegments(items: AllocationItem[]): AllocationSegment[] {
  const sorted = [...items].sort((a, b) => new Decimal(b.percentage).comparedTo(new Decimal(a.percentage)))

  const segments: AllocationSegment[] = []
  let otherPct = new Decimal(0)

  for (const item of sorted) {
    const pct = new Decimal(item.percentage)
    if (pct.lessThan(OTHER_THRESHOLD_PCT)) {
      otherPct = otherPct.plus(pct)
    } else {
      segments.push({ key: item.asset_id, symbol: item.symbol, pct, color: emblemColor(item.symbol) })
    }
  }

  if (otherPct.greaterThan(0)) {
    segments.push({ key: '__other__', symbol: 'Other', pct: otherPct, color: 'var(--text-muted)' })
  }

  return segments
}

export default function AllocationBar({ items }: Props) {
  if (items.length === 0) {
    return (
      <p role="note" className="muted-text">
        No priced allocation to summarize.
      </p>
    )
  }

  const segments = buildSegments(items)

  return (
    <div className="allocation-summary">
      <div className="allocation-summary-bar" aria-hidden="true">
        {segments.map((seg) => (
          <span
            key={seg.key}
            className="allocation-summary-segment"
            style={{ width: `${seg.pct.toFixed(2)}%`, background: seg.color }}
          />
        ))}
      </div>
      <div className="allocation-summary-legend">
        {segments.map((seg) => (
          <div key={seg.key} className="allocation-summary-legend-item">
            <i className="allocation-summary-swatch" style={{ background: seg.color }} />
            <span className="allocation-summary-symbol">{seg.symbol}</span>
            <b className="allocation-summary-pct">{seg.pct.toFixed(1)}%</b>
          </div>
        ))}
      </div>
    </div>
  )
}
