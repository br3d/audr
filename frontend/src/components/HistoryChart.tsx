import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ReferenceLine,
  ResponsiveContainer,
} from 'recharts'
import { Decimal } from 'decimal.js'
import type { HistoryPoint } from '../api/client'
import HistoryTable from './HistoryTable'

interface Props {
  points: HistoryPoint[]
}

interface ChartDatum {
  snapshotted_at: string
  value: number | null
  has_gap: boolean
  quality: HistoryPoint['quality']
}

function formatTimestamp(ts: string): string {
  try {
    return new Date(ts).toLocaleString('en-US', {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    })
  } catch {
    return ts
  }
}

function formatTooltipValue(value: number | null): string {
  if (value === null) return 'unknown'
  return '$' + new Decimal(value).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

export default function HistoryChart({ points }: Props) {
  if (points.length === 0) {
    return <p role="note">No history data available for this range.</p>
  }

  const data: ChartDatum[] = points.map((p) => ({
    snapshotted_at: p.snapshotted_at,
    // parseFloat is acceptable here: chart rendering does not require decimal precision
    value: p.total_value_usd !== null ? parseFloat(p.total_value_usd) : null,
    has_gap: p.has_gap,
    quality: p.quality,
  }))

  const gapTimestamps = data.filter((d) => d.has_gap).map((d) => d.snapshotted_at)

  const tickFormatter = (ts: string) => formatTimestamp(ts)

  return (
    <div>
      <div
        role="img"
        aria-label="Portfolio value history chart"
        aria-describedby="history-chart-table-caption"
      >
        <ResponsiveContainer width="100%" height={300}>
          <LineChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 16 }}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis
              dataKey="snapshotted_at"
              tickFormatter={tickFormatter}
              tick={{ fontSize: 11 }}
              minTickGap={60}
            />
            <YAxis
              tickFormatter={(v: number) =>
                '$' + new Decimal(v).toFixed(0).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
              }
              tick={{ fontSize: 11 }}
              width={80}
            />
            <Tooltip
              formatter={(value) => [formatTooltipValue(value as number | null), 'Total']}
              labelFormatter={(label) => tickFormatter(String(label))}
            />
            {gapTimestamps.map((ts) => (
              <ReferenceLine
                key={ts}
                x={ts}
                stroke="#f59e0b"
                strokeDasharray="4 2"
                label={{ value: 'Gap', position: 'top', fontSize: 10, fill: '#92400e' }}
              />
            ))}
            <Line
              type="linear"
              dataKey="value"
              stroke="#2563eb"
              dot={false}
              connectNulls={false}
              strokeWidth={2}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>

      {gapTimestamps.length > 0 && (
        <p role="note" id="history-gap-explanation">
          Yellow dashed lines mark gaps — periods where observation data is missing. Values
          either side of a gap are independent reads.
        </p>
      )}

      <details>
        <summary id="history-chart-table-caption">
          Accessible data table ({points.length} point{points.length !== 1 ? 's' : ''})
        </summary>
        <HistoryTable points={points} />
      </details>
    </div>
  )
}
