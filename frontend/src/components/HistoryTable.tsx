import type { HistoryPoint } from '../api/client'
import MoneyValue from './MoneyValue'

interface Props {
  points: HistoryPoint[]
}

function qualityLabel(quality: HistoryPoint['quality']): string {
  if (quality === 'stale') return 'Stale'
  if (quality === 'incomplete') return 'Incomplete'
  return 'OK'
}

export default function HistoryTable({ points }: Props) {
  if (points.length === 0) {
    return <p role="note">No history data available for this range.</p>
  }

  return (
    <table aria-label="Portfolio value history">
      <thead>
        <tr>
          <th scope="col">Time</th>
          <th scope="col">Total (USD)</th>
          <th scope="col">Data quality</th>
        </tr>
      </thead>
      <tbody>
        {points.map((point) => (
          <tr key={point.snapshotted_at} aria-label={point.has_gap ? 'Gap — data missing before this point' : undefined}>
            <td>{point.snapshotted_at}</td>
            <td>
              <MoneyValue value={point.total_value_usd} />
            </td>
            <td>
              {point.has_gap && (
                <span aria-label="Gap marker" role="note" data-testid="gap-marker">
                  Gap ·{' '}
                </span>
              )}
              {qualityLabel(point.quality)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
