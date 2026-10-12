import { describe, it, expect } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import AllocationBar, { buildSegments } from '../components/AllocationBar'
import type { AllocationItem } from '../api/client'

function render(ui: React.ReactElement): { container: HTMLDivElement; root: Root } {
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  act(() => {
    root.render(ui)
  })
  return { container, root }
}

async function unmount(container: HTMLDivElement, root: Root) {
  await act(async () => { root.unmount() })
  document.body.removeChild(container)
}

function makeItem(overrides: Partial<AllocationItem> = {}): AllocationItem {
  return {
    asset_id: 'eth',
    symbol: 'ETH',
    value_usd: '3456.78',
    percentage: '75.50',
    quantity: '1.5',
    price_usd: '2304.52',
    wallet_count: 1,
    read_status: 'ok',
    included: true,
    ...overrides,
  }
}

describe('buildSegments', () => {
  it('sorts segments by descending share', () => {
    const segments = buildSegments([
      makeItem({ asset_id: 'omg', symbol: 'OMG', percentage: '25.00' }),
      makeItem({ asset_id: 'eth', symbol: 'ETH', percentage: '75.00' }),
    ])
    expect(segments.map((s) => s.symbol)).toEqual(['ETH', 'OMG'])
  })

  it('collapses shares below the threshold into a single Other bucket', () => {
    const segments = buildSegments([
      makeItem({ asset_id: 'eth', symbol: 'ETH', percentage: '97.50' }),
      makeItem({ asset_id: 'a', symbol: 'AAA', percentage: '0.80' }),
      makeItem({ asset_id: 'b', symbol: 'BBB', percentage: '0.70' }),
      makeItem({ asset_id: 'c', symbol: 'CCC', percentage: '1.00' }),
    ])
    expect(segments.map((s) => s.symbol)).toEqual(['ETH', 'CCC', 'Other'])
    const other = segments.find((s) => s.symbol === 'Other')!
    expect(other.pct.toFixed(2)).toBe('1.50')
  })

  it('does not add an Other bucket when nothing is below the threshold', () => {
    const segments = buildSegments([
      makeItem({ asset_id: 'eth', symbol: 'ETH', percentage: '60.00' }),
      makeItem({ asset_id: 'omg', symbol: 'OMG', percentage: '40.00' }),
    ])
    expect(segments.some((s) => s.symbol === 'Other')).toBe(false)
  })

  it('gives the same symbol the same color every time', () => {
    const [a] = buildSegments([makeItem({ asset_id: 'eth', symbol: 'ETH', percentage: '100.00' })])
    const [b] = buildSegments([makeItem({ asset_id: 'eth2', symbol: 'ETH', percentage: '100.00' })])
    expect(a.color).toBe(b.color)
  })
})

describe('AllocationBar', () => {
  it('renders a hidden bar segment per included asset', async () => {
    const { container, root } = render(
      <AllocationBar
        items={[
          makeItem({ asset_id: 'eth', symbol: 'ETH', percentage: '75.00' }),
          makeItem({ asset_id: 'omg', symbol: 'OMG', percentage: '25.00' }),
        ]}
      />,
    )
    const bar = container.querySelector('.allocation-summary-bar')!
    expect(bar.getAttribute('aria-hidden')).toBe('true')
    expect(bar.querySelectorAll('.allocation-summary-segment').length).toBe(2)
    await unmount(container, root)
  })

  it('shows the legend with color, symbol and percentage as readable text', async () => {
    const { container, root } = render(
      <AllocationBar items={[makeItem({ asset_id: 'eth', symbol: 'ETH', percentage: '75.50' })]} />,
    )
    expect(container.textContent).toContain('ETH')
    expect(container.textContent).toContain('75.5%')
    await unmount(container, root)
  })

  it('renders an explanatory note instead of an empty bar when there is nothing to summarize', async () => {
    const { container, root } = render(<AllocationBar items={[]} />)
    expect(container.querySelector('.allocation-summary-bar')).toBeNull()
    expect(container.textContent).toMatch(/no priced allocation/i)
    await unmount(container, root)
  })
})
