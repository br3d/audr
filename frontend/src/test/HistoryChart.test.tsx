import { describe, it, expect } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import type { HistoryPoint } from '../api/client'
import HistoryChart from '../components/HistoryChart'

const POINTS: HistoryPoint[] = [
  { timestamp: '2026-01-15T00:00:00Z', total_usd: '5000.00', quality: 'ok', gap: false },
  { timestamp: '2026-01-16T00:00:00Z', total_usd: '5200.00', quality: 'ok', gap: false },
  { timestamp: '2026-01-17T00:00:00Z', total_usd: '4900.00', quality: 'stale', gap: true },
]

const POINTS_NO_GAPS: HistoryPoint[] = [
  { timestamp: '2026-01-15T00:00:00Z', total_usd: '5000.00', quality: 'ok', gap: false },
  { timestamp: '2026-01-16T00:00:00Z', total_usd: '5100.00', quality: 'ok', gap: false },
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
})
