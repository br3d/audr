import { describe, it, expect, afterEach } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import type { HistoryPoint } from '../api/client'
import HistoryTable from '../components/HistoryTable'

const POINT_OK: HistoryPoint = {
  snapshot_id: 'abc-ok',
  snapshotted_at: '2026-01-15T12:00:00Z',
  total_value_usd: '5000.00',
  quality: 'ok',
  included_wallet_count: 1,
  included_asset_count: 1,
  has_gap: false,
  is_canonical: true,
  is_gap_marker: false,
}

const POINT_GAP: HistoryPoint = {
  snapshot_id: 'abc-gap',
  snapshotted_at: '2026-01-16T12:00:00Z',
  total_value_usd: '4800.50',
  quality: 'stale',
  included_wallet_count: 1,
  included_asset_count: 1,
  has_gap: true,
  is_canonical: true,
  is_gap_marker: false,
}

const POINT_INCOMPLETE: HistoryPoint = {
  snapshot_id: 'abc-incomplete',
  snapshotted_at: '2026-01-17T12:00:00Z',
  total_value_usd: null,
  quality: 'incomplete',
  included_wallet_count: 1,
  included_asset_count: 1,
  has_gap: false,
  is_canonical: true,
  is_gap_marker: false,
}

const POINT_INVALIDATED: HistoryPoint = {
  snapshot_id: 'abc-invalidated',
  snapshotted_at: '2026-01-18T12:00:00Z',
  total_value_usd: '4900.00',
  quality: 'ok',
  included_wallet_count: 1,
  included_asset_count: 1,
  has_gap: false,
  is_canonical: false,
  is_gap_marker: false,
}

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

describe('HistoryTable', () => {
  afterEach(() => {})

  it('shows empty state note when points array is empty', async () => {
    const { container, root } = mount(React.createElement(HistoryTable, { points: [] }))
    expect(container.textContent).toMatch(/no history data/i)
    await unmount(container, root)
  })

  it('renders a table with correct aria-label', async () => {
    const { container, root } = mount(
      React.createElement(HistoryTable, { points: [POINT_OK] }),
    )
    const table = container.querySelector('[aria-label="Portfolio value history"]')
    expect(table).toBeTruthy()
    await unmount(container, root)
  })

  it('displays formatted USD value for priced point', async () => {
    const { container, root } = mount(
      React.createElement(HistoryTable, { points: [POINT_OK] }),
    )
    expect(container.textContent).toContain('$5,000.00')
    await unmount(container, root)
  })

  it('shows "unknown" for null total_value_usd', async () => {
    const { container, root } = mount(
      React.createElement(HistoryTable, { points: [POINT_INCOMPLETE] }),
    )
    expect(container.textContent).toMatch(/unknown/i)
    await unmount(container, root)
  })

  it('renders gap marker for gap points', async () => {
    const { container, root } = mount(
      React.createElement(HistoryTable, { points: [POINT_GAP] }),
    )
    const gapMarker = container.querySelector('[data-testid="gap-marker"]')
    expect(gapMarker).toBeTruthy()
    await unmount(container, root)
  })

  it('does not render gap marker for non-gap points', async () => {
    const { container, root } = mount(
      React.createElement(HistoryTable, { points: [POINT_OK] }),
    )
    const gapMarker = container.querySelector('[data-testid="gap-marker"]')
    expect(gapMarker).toBeNull()
    await unmount(container, root)
  })

  it('shows "Stale" quality label', async () => {
    const { container, root } = mount(
      React.createElement(HistoryTable, { points: [POINT_GAP] }),
    )
    expect(container.textContent).toContain('Stale')
    await unmount(container, root)
  })

  it('shows "Incomplete" quality label', async () => {
    const { container, root } = mount(
      React.createElement(HistoryTable, { points: [POINT_INCOMPLETE] }),
    )
    expect(container.textContent).toContain('Incomplete')
    await unmount(container, root)
  })

  it('shows "OK" quality label for ok quality', async () => {
    const { container, root } = mount(
      React.createElement(HistoryTable, { points: [POINT_OK] }),
    )
    expect(container.textContent).toContain('OK')
    await unmount(container, root)
  })

  it('renders one row per point', async () => {
    const points = [POINT_OK, POINT_GAP, POINT_INCOMPLETE]
    const { container, root } = mount(
      React.createElement(HistoryTable, { points }),
    )
    const tbody = container.querySelector('tbody')
    expect(tbody?.querySelectorAll('tr').length).toBe(3)
    await unmount(container, root)
  })

  it('renders invalidated marker for non-canonical points', async () => {
    const { container, root } = mount(
      React.createElement(HistoryTable, { points: [POINT_INVALIDATED] }),
    )
    const marker = container.querySelector('[data-testid="invalidated-marker"]')
    expect(marker).toBeTruthy()
    await unmount(container, root)
  })

  it('does not render invalidated marker for canonical points', async () => {
    const { container, root } = mount(
      React.createElement(HistoryTable, { points: [POINT_OK] }),
    )
    const marker = container.querySelector('[data-testid="invalidated-marker"]')
    expect(marker).toBeNull()
    await unmount(container, root)
  })

  it('labels the row as invalidated for non-canonical points', async () => {
    const { container, root } = mount(
      React.createElement(HistoryTable, { points: [POINT_INVALIDATED] }),
    )
    const row = container.querySelector('tbody tr')
    expect(row?.getAttribute('aria-label')).toMatch(/invalidated/i)
    await unmount(container, root)
  })

  it('contains no PnL terminology', async () => {
    const { container, root } = mount(
      React.createElement(HistoryTable, { points: [POINT_OK, POINT_GAP] }),
    )
    const text = container.textContent?.toLowerCase() ?? ''
    expect(text).not.toContain('profit')
    expect(text).not.toContain('loss')
    expect(text).not.toContain('pnl')
    expect(text).not.toContain('p&l')
    expect(text).not.toContain('gain')
    await unmount(container, root)
  })
})
