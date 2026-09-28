import { describe, it, expect } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import AllocationTable from '../components/AllocationTable'
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

describe('AllocationTable', () => {
  describe('empty state', () => {
    it('renders an explanatory note when items array is empty', async () => {
      const { container, root } = render(<AllocationTable items={[]} />)
      const note = container.querySelector('[role="note"]')
      expect(note).toBeTruthy()
      expect(note!.textContent).toMatch(/no allocation/i)
      await unmount(container, root)
    })

    it('does not render a table when items array is empty', async () => {
      const { container, root } = render(<AllocationTable items={[]} />)
      expect(container.querySelector('table')).toBeNull()
      await unmount(container, root)
    })
  })

  describe('data rows', () => {
    it('renders one row per item', async () => {
      const { container, root } = render(<AllocationTable items={ITEMS} />)
      const rows = container.querySelectorAll('tbody tr')
      expect(rows.length).toBe(2)
      await unmount(container, root)
    })

    it('displays the symbol in each row', async () => {
      const { container, root } = render(<AllocationTable items={ITEMS} />)
      expect(container.textContent).toContain('ETH')
      expect(container.textContent).toContain('USDC')
      await unmount(container, root)
    })

    it('displays formatted USD values using MoneyValue', async () => {
      const { container, root } = render(<AllocationTable items={ITEMS} />)
      // MoneyValue formats 3456.78 → $3,456.78
      expect(container.textContent).toContain('$3,456.78')
      expect(container.textContent).toContain('$1,122.00')
      await unmount(container, root)
    })

    it('displays percentage values with a % suffix', async () => {
      const { container, root } = render(<AllocationTable items={ITEMS} />)
      expect(container.textContent).toContain('75.50%')
      expect(container.textContent).toContain('24.50%')
      await unmount(container, root)
    })
  })

  describe('accessibility', () => {
    it('table has accessible aria-label', async () => {
      const { container, root } = render(<AllocationTable items={ITEMS} />)
      const table = container.querySelector('table')!
      expect(table.getAttribute('aria-label')).toBeTruthy()
      await unmount(container, root)
    })

    it('column headers use scope="col"', async () => {
      const { container, root } = render(<AllocationTable items={ITEMS} />)
      const headers = container.querySelectorAll('th[scope="col"]')
      expect(headers.length).toBeGreaterThanOrEqual(3)
      await unmount(container, root)
    })

    it('percentage cells have aria-label with "percent" text', async () => {
      const { container, root } = render(<AllocationTable items={ITEMS} />)
      const percSpans = container.querySelectorAll('span[aria-label*="percent"]')
      expect(percSpans.length).toBe(2)
      await unmount(container, root)
    })
  })
})
