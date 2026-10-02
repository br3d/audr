import { describe, it, expect } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import AllocationList, { splitAllocations } from '../components/AllocationList'
import { monogram } from '../components/AssetEmblem'
import type { AllocationItem } from '../api/client'

function render(ui: React.ReactElement): { container: HTMLDivElement; root: Root } {
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  act(() => { root.render(ui) })
  return { container, root }
}

async function unmount(container: HTMLDivElement, root: Root) {
  await act(async () => { root.unmount() })
  document.body.removeChild(container)
}

const ITEMS: AllocationItem[] = [
  { asset_id: 'eth', symbol: 'ETH', value_usd: '3456.78', percentage: '75.50' },
  { asset_id: 'usdc', symbol: 'USDC', value_usd: '1122.00', percentage: '24.50' },
]

/** Two significant holdings plus a long tail of dust — exercises the spoiler. */
function dustyPortfolio(dustCount: number): AllocationItem[] {
  const items: AllocationItem[] = [
    { asset_id: 'eth', symbol: 'ETH', value_usd: '9000.00', percentage: '90.00' },
    { asset_id: 'usdc', symbol: 'USDC', value_usd: '500.00', percentage: '5.00' },
  ]
  for (let i = 0; i < dustCount; i += 1) {
    items.push({
      asset_id: `dust-${i}`,
      symbol: `DST${i}`,
      value_usd: '10.00',
      percentage: '0.10',
    })
  }
  return items
}

describe('AllocationList', () => {
  describe('empty state', () => {
    it('renders an explanatory note when items array is empty', async () => {
      const { container, root } = render(<AllocationList items={[]} />)
      const note = container.querySelector('[role="note"]')
      expect(note).toBeTruthy()
      expect(note!.textContent).toMatch(/no allocation/i)
      await unmount(container, root)
    })

    it('does not render a table when items array is empty', async () => {
      const { container, root } = render(<AllocationList items={[]} />)
      expect(container.querySelector('table')).toBeNull()
      await unmount(container, root)
    })
  })

  describe('data rows', () => {
    it('renders one row per item', async () => {
      const { container, root } = render(<AllocationList items={ITEMS} />)
      expect(container.querySelectorAll('tbody tr').length).toBe(2)
      await unmount(container, root)
    })

    it('displays the symbol in each row', async () => {
      const { container, root } = render(<AllocationList items={ITEMS} />)
      expect(container.textContent).toContain('ETH')
      expect(container.textContent).toContain('USDC')
      await unmount(container, root)
    })

    it('displays formatted USD values using MoneyValue', async () => {
      const { container, root } = render(<AllocationList items={ITEMS} />)
      expect(container.textContent).toContain('$3,456.78')
      expect(container.textContent).toContain('$1,122.00')
      await unmount(container, root)
    })

    it('displays percentage values with a % suffix', async () => {
      const { container, root } = render(<AllocationList items={ITEMS} />)
      expect(container.textContent).toContain('75.50%')
      expect(container.textContent).toContain('24.50%')
      await unmount(container, root)
    })

    it('sorts rows by descending share regardless of input order', async () => {
      const unsorted: AllocationItem[] = [ITEMS[1], ITEMS[0]]
      const { container, root } = render(<AllocationList items={unsorted} />)
      const first = container.querySelector('tbody tr')!
      expect(first.textContent).toContain('ETH')
      await unmount(container, root)
    })

    it('sizes the share bar from the percentage', async () => {
      const { container, root } = render(<AllocationList items={ITEMS} />)
      const fill = container.querySelector('.allocation-bar-fill') as HTMLElement
      expect(fill.style.width).toBe('75.5%')
      await unmount(container, root)
    })
  })

  describe('asset emblem', () => {
    it('renders a monogram emblem for every row when no logo is available', async () => {
      const { container, root } = render(<AllocationList items={ITEMS} />)
      const emblems = container.querySelectorAll('.asset-emblem-monogram')
      expect(emblems.length).toBe(2)
      expect(emblems[0].textContent).toBe('ETH')
      await unmount(container, root)
    })

    it('renders an image emblem when logo_url is present', async () => {
      const withLogo: AllocationItem[] = [
        { ...ITEMS[0], logo_url: 'https://example.test/eth.png' },
      ]
      const { container, root } = render(<AllocationList items={withLogo} />)
      const img = container.querySelector('img.asset-emblem') as HTMLImageElement
      expect(img).toBeTruthy()
      expect(img.getAttribute('src')).toBe('https://example.test/eth.png')
      await unmount(container, root)
    })

    it('hides emblems from assistive technology (symbol text carries the name)', async () => {
      const { container, root } = render(<AllocationList items={ITEMS} />)
      const emblem = container.querySelector('.asset-emblem')!
      expect(emblem.getAttribute('aria-hidden')).toBe('true')
      await unmount(container, root)
    })

    it('monogram is at most three alphanumeric characters', () => {
      expect(monogram('ETH')).toBe('ETH')
      expect(monogram('1INCH')).toBe('1IN')
      expect(monogram('T')).toBe('T')
      expect(monogram('-')).toBe('?')
    })
  })

  describe('small-share spoiler', () => {
    it('collapses the dust tail behind a toggle', async () => {
      const { container, root } = render(<AllocationList items={dustyPortfolio(10)} />)
      const toggle = container.querySelector('.allocation-spoiler') as HTMLButtonElement
      expect(toggle).toBeTruthy()
      expect(toggle.getAttribute('aria-expanded')).toBe('false')
      // Floor of 5 visible rows: ETH, USDC and three dust entries.
      expect(container.querySelectorAll('tbody tr').length).toBe(5)
      expect(toggle.textContent).toContain('7 smaller assets')
      await unmount(container, root)
    })

    it('summarises the hidden value and share on the collapsed toggle', async () => {
      const { container, root } = render(<AllocationList items={dustyPortfolio(10)} />)
      const toggle = container.querySelector('.allocation-spoiler')!
      // 7 hidden dust rows at $10.00 / 0.10% each.
      expect(toggle.textContent).toContain('$70.00')
      expect(toggle.textContent).toContain('0.70%')
      await unmount(container, root)
    })

    it('reveals the hidden rows when the toggle is activated', async () => {
      const { container, root } = render(<AllocationList items={dustyPortfolio(10)} />)
      const toggle = container.querySelector('.allocation-spoiler') as HTMLButtonElement
      await act(async () => { toggle.click() })
      expect(container.querySelectorAll('tbody tr').length).toBe(12)
      expect(toggle.getAttribute('aria-expanded')).toBe('true')
      expect(toggle.textContent).toContain('Hide')
      await unmount(container, root)
    })

    it('does not render a spoiler when only a couple of rows would be hidden', async () => {
      const { container, root } = render(<AllocationList items={dustyPortfolio(4)} />)
      expect(container.querySelector('.allocation-spoiler')).toBeNull()
      expect(container.querySelectorAll('tbody tr').length).toBe(6)
      await unmount(container, root)
    })

    it('caps the collapsed view at twelve rows even when all shares are significant', () => {
      const many: AllocationItem[] = Array.from({ length: 30 }, (_, i) => ({
        asset_id: `a-${i}`,
        symbol: `A${i}`,
        value_usd: '100.00',
        percentage: '3.33',
      }))
      const { visible, hidden } = splitAllocations(many)
      expect(visible.length).toBe(12)
      expect(hidden.length).toBe(18)
    })

    it('keeps a five-row floor when every share is tiny', () => {
      const dust: AllocationItem[] = Array.from({ length: 20 }, (_, i) => ({
        asset_id: `d-${i}`,
        symbol: `D${i}`,
        value_usd: '1.00',
        percentage: '0.05',
      }))
      const { visible, hidden } = splitAllocations(dust)
      expect(visible.length).toBe(5)
      expect(hidden.length).toBe(15)
    })
  })

  describe('accessibility', () => {
    it('table has accessible aria-label', async () => {
      const { container, root } = render(<AllocationList items={ITEMS} />)
      expect(container.querySelector('table')!.getAttribute('aria-label')).toBeTruthy()
      await unmount(container, root)
    })

    it('column headers use scope="col"', async () => {
      const { container, root } = render(<AllocationList items={ITEMS} />)
      expect(container.querySelectorAll('th[scope="col"]').length).toBeGreaterThanOrEqual(3)
      await unmount(container, root)
    })

    it('percentage cells have aria-label with "percent" text', async () => {
      const { container, root } = render(<AllocationList items={ITEMS} />)
      expect(container.querySelectorAll('span[aria-label*="percent"]').length).toBe(2)
      await unmount(container, root)
    })
  })
})
