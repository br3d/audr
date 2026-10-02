import { describe, it, expect } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import type { HistoryPoint } from '../api/client'
import HistoryChart, { buildChartData, computeYDomain, formatAxisUSD } from '../components/HistoryChart'

const POINTS: HistoryPoint[] = [
  {
    snapshot_id: 'abc-1',
    snapshotted_at: '2026-01-15T00:00:00Z',
    total_value_usd: '5000.00',
    quality: 'ok',
    included_wallet_count: 1,
    included_asset_count: 1,
    has_gap: false,
    is_canonical: true,
    is_gap_marker: false,
  },
  {
    snapshot_id: 'abc-2',
    snapshotted_at: '2026-01-16T00:00:00Z',
    total_value_usd: '5200.00',
    quality: 'ok',
    included_wallet_count: 1,
    included_asset_count: 1,
    has_gap: false,
    is_canonical: true,
    is_gap_marker: false,
  },
  {
    snapshot_id: null,
    snapshotted_at: '2026-01-17T00:00:00Z',
    total_value_usd: null,
    quality: 'stale',
    included_wallet_count: 0,
    included_asset_count: 0,
    has_gap: true,
    is_canonical: false,
    is_gap_marker: true,
  },
]

const POINTS_NO_GAPS: HistoryPoint[] = [
  {
    snapshot_id: 'abc-3',
    snapshotted_at: '2026-01-15T00:00:00Z',
    total_value_usd: '5000.00',
    quality: 'ok',
    included_wallet_count: 1,
    included_asset_count: 1,
    has_gap: false,
    is_canonical: true,
    is_gap_marker: false,
  },
  {
    snapshot_id: 'abc-4',
    snapshotted_at: '2026-01-16T00:00:00Z',
    total_value_usd: '5100.00',
    quality: 'ok',
    included_wallet_count: 1,
    included_asset_count: 1,
    has_gap: false,
    is_canonical: true,
    is_gap_marker: false,
  },
]

function mount(element: React.ReactElement): { container: HTMLDivElement; root: Root } {
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  act(() => {
    root.render(element)
  })
  return { container, root }
}

async function unmount(container: HTMLDivElement, root: Root) {
  await act(async () => {
    root.unmount()
  })
  document.body.removeChild(container)
}

describe('HistoryChart', () => {
  it('shows empty state note when points array is empty', async () => {
    const { container, root } = mount(React.createElement(HistoryChart, { points: [] }))
    expect(container.textContent).toMatch(/no history data/i)
    await unmount(container, root)
  })

  it('renders chart container with accessible aria-label', async () => {
    const { container, root } = mount(
      React.createElement(HistoryChart, { points: POINTS }),
    )
    const chartEl = container.querySelector('[aria-label="Portfolio value history chart"]')
    expect(chartEl).toBeTruthy()
    await unmount(container, root)
  })

  it('renders accessible data table via details/summary', async () => {
    const { container, root } = mount(
      React.createElement(HistoryChart, { points: POINTS }),
    )
    const details = container.querySelector('details')
    expect(details).toBeTruthy()
    const table = container.querySelector('[aria-label="Portfolio value history"]')
    expect(table).toBeTruthy()
    await unmount(container, root)
  })

  it('shows gap explanation note when gap points exist', async () => {
    const { container, root } = mount(
      React.createElement(HistoryChart, { points: POINTS }),
    )
    expect(container.textContent).toMatch(/gap/i)
    await unmount(container, root)
  })

  it('does not show gap explanation note when no gaps', async () => {
    const { container, root } = mount(
      React.createElement(HistoryChart, { points: POINTS_NO_GAPS }),
    )
    // Gap explanation paragraph should be absent (no reference lines or note)
    const gapNote = Array.from(container.querySelectorAll('[role="note"]')).find((el) =>
      el.textContent?.toLowerCase().includes('gap'),
    )
    expect(gapNote).toBeUndefined()
    await unmount(container, root)
  })

  it('contains no PnL terminology', async () => {
    const { container, root } = mount(
      React.createElement(HistoryChart, { points: POINTS }),
    )
    const text = container.textContent?.toLowerCase() ?? ''
    expect(text).not.toContain('profit')
    expect(text).not.toContain('loss')
    expect(text).not.toContain('pnl')
    expect(text).not.toContain('p&l')
    expect(text).not.toContain('gain')
    await unmount(container, root)
  })

  it('accessible table caption mentions point count', async () => {
    const { container, root } = mount(
      React.createElement(HistoryChart, { points: POINTS }),
    )
    const summary = container.querySelector('summary')
    expect(summary?.textContent).toMatch(/3 points/i)
    await unmount(container, root)
  })

  it('buildChartData sorts oldest-first from newest-first input', () => {
    const newestFirst: HistoryPoint[] = [
      {
        snapshot_id: 'n1',
        snapshotted_at: '2026-03-03T00:00:00Z',
        total_value_usd: '3000.00',
        quality: 'ok',
        included_wallet_count: 1,
        included_asset_count: 1,
        has_gap: false,
        is_canonical: true,
        is_gap_marker: false,
      },
      {
        snapshot_id: 'n2',
        snapshotted_at: '2026-03-02T00:00:00Z',
        total_value_usd: '2000.00',
        quality: 'ok',
        included_wallet_count: 1,
        included_asset_count: 1,
        has_gap: false,
        is_canonical: true,
        is_gap_marker: false,
      },
      {
        snapshot_id: 'n3',
        snapshotted_at: '2026-03-01T00:00:00Z',
        total_value_usd: '1000.00',
        quality: 'ok',
        included_wallet_count: 1,
        included_asset_count: 1,
        has_gap: false,
        is_canonical: true,
        is_gap_marker: false,
      },
    ]
    const data = buildChartData(newestFirst)
    expect(data[0].snapshotted_at).toBe('2026-03-01T00:00:00Z')
    expect(data[data.length - 1].snapshotted_at).toBe('2026-03-03T00:00:00Z')
  })

  it('buildChartData does not mutate the input array', () => {
    const newestFirst: HistoryPoint[] = [
      {
        snapshot_id: 'x1',
        snapshotted_at: '2026-03-02T00:00:00Z',
        total_value_usd: '2000.00',
        quality: 'ok',
        included_wallet_count: 1,
        included_asset_count: 1,
        has_gap: false,
        is_canonical: true,
        is_gap_marker: false,
      },
      {
        snapshot_id: 'x2',
        snapshotted_at: '2026-03-01T00:00:00Z',
        total_value_usd: '1000.00',
        quality: 'ok',
        included_wallet_count: 1,
        included_asset_count: 1,
        has_gap: false,
        is_canonical: true,
        is_gap_marker: false,
      },
    ]
    buildChartData(newestFirst)
    expect(newestFirst[0].snapshotted_at).toBe('2026-03-02T00:00:00Z')
  })

  it('buildChartData breaks the line at has_gap points instead of keeping a value', () => {
    const withGapValue: HistoryPoint[] = [
      {
        snapshot_id: 'g1',
        snapshotted_at: '2026-01-01T00:00:00Z',
        total_value_usd: '191000.00',
        quality: 'ok',
        included_wallet_count: 1,
        included_asset_count: 1,
        has_gap: false,
        is_canonical: true,
        is_gap_marker: false,
      },
      {
        // A real row can carry both a value and has_gap=true (degraded 'gaps' quality) —
        // the chart must still render it as a break, not a connected point.
        snapshot_id: 'g2',
        snapshotted_at: '2026-01-02T00:00:00Z',
        total_value_usd: '189000.00',
        quality: 'incomplete',
        included_wallet_count: 1,
        included_asset_count: 1,
        has_gap: true,
        is_canonical: true,
        is_gap_marker: false,
      },
    ]
    const data = buildChartData(withGapValue)
    expect(data[1].has_gap).toBe(true)
    expect(data[1].value).toBeNull()
  })

  describe('computeYDomain', () => {
    it('does not clamp to zero when the minimum value is far from zero', () => {
      const domain = computeYDomain([180_000, 191_000, 195_000, 188_000])
      expect(domain[0]).not.toBe(0)
      expect(domain[0]).toBeGreaterThan(0)
      expect(domain[0]).toBeLessThan(180_000)
      expect(domain[1]).toBeGreaterThan(195_000)
    })

    it('still pads a flat series without collapsing to a zero-width domain', () => {
      const domain = computeYDomain([5000, 5000, 5000])
      expect(domain[0]).toBeLessThan(5000)
      expect(domain[1]).toBeGreaterThan(5000)
    })

    it('returns a default domain for empty input', () => {
      expect(computeYDomain([])).toEqual([0, 1])
    })
  })

  describe('formatAxisUSD', () => {
    it('abbreviates thousands as k', () => {
      expect(formatAxisUSD(103_000)).toBe('$103k')
      expect(formatAxisUSD(75_000)).toBe('$75k')
    })

    it('abbreviates millions as M', () => {
      expect(formatAxisUSD(1_200_000)).toBe('$1.2M')
    })

    it('leaves sub-thousand values unabbreviated', () => {
      expect(formatAxisUSD(500)).toBe('$500')
    })
  })

  it('renders no per-gap reference lines even with many gaps', async () => {
    const manyGapPoints: HistoryPoint[] = Array.from({ length: 20 }, (_, i) => ({
      snapshot_id: i % 2 === 0 ? `p${i}` : null,
      snapshotted_at: new Date(2026, 0, i + 1).toISOString(),
      total_value_usd: i % 2 === 0 ? '191000.00' : null,
      quality: i % 2 === 0 ? 'ok' : 'stale',
      included_wallet_count: i % 2 === 0 ? 1 : 0,
      included_asset_count: i % 2 === 0 ? 1 : 0,
      has_gap: i % 2 !== 0,
      is_canonical: i % 2 === 0,
      is_gap_marker: i % 2 !== 0,
    }))
    const { container, root } = mount(
      React.createElement(HistoryChart, { points: manyGapPoints }),
    )
    expect(container.querySelectorAll('.recharts-reference-line').length).toBe(0)
    await unmount(container, root)
  })

  it('renders a hollow marker for non-canonical points and an explanation note', async () => {
    const withInvalidated: HistoryPoint[] = [
      {
        snapshot_id: 'inv-1',
        snapshotted_at: '2026-01-15T00:00:00Z',
        total_value_usd: '5000.00',
        quality: 'ok',
        included_wallet_count: 1,
        included_asset_count: 1,
        has_gap: false,
        is_canonical: false,
        is_gap_marker: false,
      },
      {
        snapshot_id: 'inv-2',
        snapshotted_at: '2026-01-16T00:00:00Z',
        total_value_usd: '5200.00',
        quality: 'ok',
        included_wallet_count: 1,
        included_asset_count: 1,
        has_gap: false,
        is_canonical: true,
        is_gap_marker: false,
      },
    ]
    const { container, root } = mount(
      React.createElement(HistoryChart, { points: withInvalidated }),
    )
    expect(container.querySelector('[data-testid="invalidated-point"]')).toBeTruthy()
    expect(container.textContent?.toLowerCase()).toContain('invalidated')
    await unmount(container, root)
  })

  it('does not render a hollow marker or note when all points are canonical', async () => {
    const { container, root } = mount(
      React.createElement(HistoryChart, { points: POINTS_NO_GAPS }),
    )
    expect(container.querySelector('[data-testid="invalidated-point"]')).toBeNull()
    expect(container.textContent?.toLowerCase()).not.toContain('invalidated')
    await unmount(container, root)
  })

  it('renders an area chart (filled series), not a bare line chart', async () => {
    const { container, root } = mount(
      React.createElement(HistoryChart, { points: POINTS }),
    )
    expect(container.querySelector('.recharts-area-area')).toBeTruthy()
    await unmount(container, root)
  })

  it('formats X-axis ticks as time for a 24h period and as a date otherwise', async () => {
    const { container: hourly, root: hourlyRoot } = mount(
      React.createElement(HistoryChart, { points: POINTS_NO_GAPS, period: '24h' }),
    )
    const hourlyTicks = Array.from(
      hourly.querySelectorAll('.recharts-xAxis-tick-labels text'),
    ).map((el) => el.textContent)
    expect(hourlyTicks.some((t) => /:\d{2}/.test(t ?? ''))).toBe(true)
    await unmount(hourly, hourlyRoot)

    const { container: monthly, root: monthlyRoot } = mount(
      React.createElement(HistoryChart, { points: POINTS_NO_GAPS, period: '30d' }),
    )
    const monthlyTicks = Array.from(
      monthly.querySelectorAll('.recharts-xAxis-tick-labels text'),
    ).map((el) => el.textContent)
    expect(monthlyTicks.every((t) => !/:\d{2}/.test(t ?? ''))).toBe(true)
    await unmount(monthly, monthlyRoot)
  })
})
