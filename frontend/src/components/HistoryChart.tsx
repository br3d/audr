import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from 'recharts'
import type { DotItemDotProps } from 'recharts/types/util/types'
import { Decimal } from 'decimal.js'
import type { HistoryPoint, HistoryPeriod } from '../api/client'
import HistoryTable from './HistoryTable'

interface Props {
  points: HistoryPoint[]
  period?: HistoryPeriod
}

interface ChartDatum {
  snapshotted_at: string
  value: number | null
  has_gap: boolean
  quality: HistoryPoint['quality']
  is_canonical: boolean
}

const LINE_COLOR = '#4a6cf7'
const GRADIENT_ID = 'historyChartFill'

function formatTimestamp(ts: string, period: HistoryPeriod): string {
  try {
    const date = new Date(ts)
    if (period === '24h') {
      return date.toLocaleString('en-US', {
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      })
    }
    return date.toLocaleString('en-US', { month: 'short', day: 'numeric' })
  } catch {
    return ts
  }
}

function formatTooltipValue(value: number | null): string {
  if (value === null || value === undefined) return 'unknown'
  return '$' + new Decimal(value).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

export function formatAxisUSD(value: number): string {
  const sign = value < 0 ? '-' : ''
  const abs = Math.abs(value)
  if (abs >= 1_000_000) {
    const millions = abs / 1_000_000
    const formatted = millions >= 10 ? millions.toFixed(0) : millions.toFixed(1).replace(/\.0$/, '')
    return `${sign}$${formatted}M`
  }
  if (abs >= 1_000) {
    return `${sign}$${Math.round(abs / 1000)}k`
  }
  return `${sign}$${Math.round(abs)}`
}

function niceStep(rawStep: number): number {
  if (!(rawStep > 0)) return 1
  const exponent = Math.floor(Math.log10(rawStep))
  const fraction = rawStep / Math.pow(10, exponent)
  let niceFraction: number
  if (fraction <= 1) niceFraction = 1
  else if (fraction <= 2) niceFraction = 2
  else if (fraction <= 5) niceFraction = 5
  else niceFraction = 10
  return niceFraction * Math.pow(10, exponent)
}

function niceFloor(value: number, step: number): number {
  return Math.floor(value / step) * step
}

function niceCeil(value: number, step: number): number {
  return Math.ceil(value / step) * step
}

/** Pads the data range ~5% below the minimum and above the maximum, then
 * rounds both edges out to a "nice" step so axis ticks don't look arbitrary.
 * Never clamps to zero — a flat-ish series near $191k should stay near $191k. */
export function computeYDomain(values: number[]): [number, number] {
  if (values.length === 0) return [0, 1]
  const min = Math.min(...values)
  const max = Math.max(...values)
  const range = max - min
  const padding = range > 0 ? range * 0.05 : Math.abs(min) * 0.05 || 1
  const step = niceStep(range > 0 ? range / 4 : padding)
  return [niceFloor(min - padding, step), niceCeil(max + padding, step)]
}

export function buildChartData(points: HistoryPoint[]): ChartDatum[] {
  return [...points]
    .sort((a, b) => a.snapshotted_at.localeCompare(b.snapshotted_at))
    .map((p) => ({
      snapshotted_at: p.snapshotted_at,
      // Only a genuinely valueless entry breaks the line (null). Synthetic gap
      // markers carry total_value_usd === null, so they break it here.
      // `has_gap` must NOT null the value: it is a data-quality annotation
      // meaning "some asset values in this snapshot were unknown", and such a
      // point still has a valid total. Treating it as a break blanked the whole
      // chart whenever every snapshot was partial (AUD-371).
      // parseFloat is acceptable here: chart rendering does not require decimal precision.
      value: p.total_value_usd === null ? null : parseFloat(p.total_value_usd),
      has_gap: p.has_gap,
      quality: p.quality,
      is_canonical: p.is_canonical,
    }))
}

function findLastValueIndex(data: ChartDatum[]): number {
  for (let i = data.length - 1; i >= 0; i -= 1) {
    if (data[i].value !== null) return i
  }
  return -1
}

export default function HistoryChart({ points, period = '30d' }: Props) {
  if (points.length === 0) {
    return <p role="note">No history data available for this range.</p>
  }

  const data = buildChartData(points)
  // Driven by real breaks in the drawn line, not by the has_gap annotation —
  // otherwise the legend promises breaks that aren't there.
  const hasLineBreaks = data.some((d) => d.value === null)
  const hasPartialPoints = data.some((d) => d.has_gap && d.value !== null)
  const hasInvalidated = data.some((d) => !d.is_canonical && d.value !== null)
  const values = data
    .map((d) => d.value)
    .filter((v): v is number => v !== null)
  const domain = computeYDomain(values)
  const lastValueIndex = findLastValueIndex(data)

  const tickFormatter = (ts: string) => formatTimestamp(ts, period)

  const endDot = (props: DotItemDotProps) => {
    const datum = data[props.index ?? -1] as ChartDatum | undefined
    if (props.value === null || !datum) {
      return <g key={props.index} />
    }
    // Invalidated points get a hollow marker — shape, not color, carries the
    // meaning, so it reads correctly without relying on color perception.
    if (!datum.is_canonical) {
      return (
        <circle
          key={props.index}
          cx={props.cx}
          cy={props.cy}
          r={4}
          fill="#fff"
          stroke={LINE_COLOR}
          strokeWidth={2}
          strokeDasharray="2 1"
          data-testid="invalidated-point"
        />
      )
    }
    if (props.index !== lastValueIndex) {
      return <g key={props.index} />
    }
    return (
      <circle
        key={props.index}
        cx={props.cx}
        cy={props.cy}
        r={4}
        fill={LINE_COLOR}
        stroke="#fff"
        strokeWidth={1.5}
      />
    )
  }

  return (
    <div>
      <div className="history-chart-legend">
        <span className="history-chart-legend-item">
          <span
            className="history-chart-legend-swatch"
            style={{ background: LINE_COLOR }}
            aria-hidden="true"
          />
          Portfolio value (USD)
        </span>
        {hasLineBreaks && (
          <span className="history-chart-legend-gap" role="note">
            Breaks in the line mark gaps — periods with missing data
          </span>
        )}
        {hasPartialPoints && (
          <span className="history-chart-legend-gap" role="note">
            Some points are partial — a few asset values were unknown when the
            snapshot was taken, so the total is an underestimate
          </span>
        )}
        {hasInvalidated && (
          <span className="history-chart-legend-gap" role="note">
            Hollow markers show invalidated points — recalculated after a reorg or
            failed verification
          </span>
        )}
      </div>

      <div
        role="img"
        aria-label="Portfolio value history chart"
        aria-describedby="history-chart-table-caption"
      >
        <ResponsiveContainer width="100%" height={300}>
          <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 8, left: 8 }}>
            <defs>
              <linearGradient id={GRADIENT_ID} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={LINE_COLOR} stopOpacity={0.28} />
                <stop offset="100%" stopColor={LINE_COLOR} stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid vertical={false} strokeDasharray="4 4" stroke="#8b8fa833" />
            <XAxis
              dataKey="snapshotted_at"
              tickFormatter={tickFormatter}
              tick={{ fontSize: 11 }}
              minTickGap={60}
              axisLine={false}
              tickLine={false}
            />
            <YAxis
              orientation="right"
              domain={domain}
              tickFormatter={(v: number) => formatAxisUSD(v)}
              tick={{ fontSize: 11 }}
              tickCount={5}
              width={56}
              axisLine={false}
              tickLine={false}
            />
            <Tooltip
              formatter={(value) => [formatTooltipValue(value as number | null), 'Total']}
              labelFormatter={(label) => tickFormatter(String(label))}
            />
            <Area
              type="monotone"
              dataKey="value"
              stroke={LINE_COLOR}
              strokeWidth={2}
              fill={`url(#${GRADIENT_ID})`}
              connectNulls={false}
              dot={endDot}
              activeDot={{ r: 4 }}
              isAnimationActive={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>

      <details>
        <summary id="history-chart-table-caption">
          Accessible data table ({points.length} point{points.length !== 1 ? 's' : ''})
        </summary>
        <HistoryTable points={points} />
      </details>
    </div>
  )
}
