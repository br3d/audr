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

const ITEMS: AllocationItem[] = [
  makeItem({ asset_id: 'eth', symbol: 'ETH', value_usd: '3456.78', percentage: '75.50' }),
  makeItem({
    asset_id: 'usdc',
    symbol: 'USDC',
    value_usd: '1122.00',
    percentage: '24.50',
    quantity: '1122.0',
    price_usd: '1.00',
  }),
]

/** Two significant holdings plus a long tail of dust — exercises the spoiler. */
function dustyPortfolio(dustCount: number): AllocationItem[] {
  const items: AllocationItem[] = [
    makeItem({ asset_id: 'eth', symbol: 'ETH', value_usd: '9000.00', percentage: '90.00' }),
    makeItem({ asset_id: 'usdc', symbol: 'USDC', value_usd: '500.00', percentage: '5.00' }),
  ]
  for (let i = 0; i < dustCount; i += 1) {
    items.push(
      makeItem({
        asset_id: `dust-${i}`,
        symbol: `DST${i}`,
        value_usd: '10.00',
        percentage: '0.10',
      }),
    )
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

    it('renders one row per asset even when it is held in multiple wallets (AUD-404 dedup)', async () => {
      // Backend aggregates per asset_id across wallets; the frontend must not
      // re-split that back into one row per wallet.
      const twoWallets = [makeItem({ asset_id: 'eth', symbol: 'ETH', wallet_count: 2 })]
      const { container, root } = render(<AllocationList items={twoWallets} />)
      expect(container.querySelectorAll('tbody tr').length).toBe(1)
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

  describe('amount column', () => {
    it('renders the quantity monospaced', async () => {
      const { container, root } = render(
        <AllocationList items={[makeItem({ quantity: '1.5' })]} />,
      )
      const cell = container.querySelector('.allocation-quantity')!
      expect(cell.textContent).toBe('1.5')
      expect(cell.className).toContain('td-mono')
      await unmount(container, root)
    })

    it('shows "unknown" when quantity is null', async () => {
      const { container, root } = render(
        <AllocationList items={[makeItem({ quantity: null })]} />,
      )
      expect(container.querySelector('.allocation-quantity')!.textContent).toBe('unknown')
      await unmount(container, root)
    })
  })

  describe('unpriced rows', () => {
    const unpriced = makeItem({
      asset_id: 'xyz',
      symbol: 'XYZ',
      value_usd: null,
      percentage: '0',
      quantity: '5.0',
      price_usd: null,
    })

    it('shows the amount plus a muted "unpriced" label instead of a value', async () => {
      const { container, root } = render(<AllocationList items={[unpriced]} />)
      expect(container.querySelector('.allocation-quantity')!.textContent).toBe('5.0')
      expect(container.querySelector('.allocation-value')!.textContent).toBe('unpriced')
      await unmount(container, root)
    })

    it('does not render a share bar or percentage for an unpriced row', async () => {
      const { container, root } = render(<AllocationList items={[unpriced]} />)
      expect(container.querySelector('.allocation-bar')).toBeNull()
      expect(container.querySelector('.allocation-pct')).toBeNull()
      await unmount(container, root)
    })

    it('sorts unpriced rows after every priced row', async () => {
      const { container, root } = render(
        <AllocationList items={[unpriced, ...ITEMS]} />,
      )
      const rows = container.querySelectorAll('tbody tr')
      expect(rows[rows.length - 1].textContent).toContain('XYZ')
      await unmount(container, root)
    })

    it('keeps unpriced rows in the hidden dust tail rather than the visible head', () => {
      const items = [...dustyPortfolio(10), unpriced]
      const { visible, hidden } = splitAllocations(items)
      expect(visible.some((i) => i.asset_id === 'xyz')).toBe(false)
      expect(hidden.some((i) => i.asset_id === 'xyz')).toBe(true)
    })
  })

  describe('read-status badge', () => {
    it.each([
      ['ok', 'Current'],
      ['stale', 'Stale'],
      ['error', 'Error'],
      ['pending', 'Pending'],
    ] as const)('renders the %s badge as "%s"', async (status, label) => {
      const { container, root } = render(
        <AllocationList items={[makeItem({ read_status: status })]} />,
      )
      expect(container.querySelector(`[aria-label="Read status: ${status}"]`)?.textContent).toBe(
        label,
      )
      await unmount(container, root)
    })
  })

  describe('excluded badge', () => {
    it('shows an "excluded" badge when included is false', async () => {
      const { container, root } = render(
        <AllocationList items={[makeItem({ included: false })]} />,
      )
      expect(container.querySelector('[aria-label="Excluded from total"]')?.textContent).toBe(
        'excluded',
      )
      await unmount(container, root)
    })

    it('does not show an excluded badge when included is true', async () => {
      const { container, root } = render(
        <AllocationList items={[makeItem({ included: true })]} />,
      )
      expect(container.querySelector('[aria-label="Excluded from total"]')).toBeNull()
      await unmount(container, root)
    })
  })

  describe('search box', () => {
    it('filters rows by symbol, case-insensitively', async () => {
      const { container, root } = render(<AllocationList items={ITEMS} />)
      const input = container.querySelector('input[aria-label="Filter allocations"]') as HTMLInputElement
      // Use native setter so React's synthetic onChange fires in jsdom.
      const nativeSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
      await act(async () => {
        nativeSetter?.call(input, 'usd')
        input.dispatchEvent(new Event('input', { bubbles: true }))
      })
      const rows = container.querySelectorAll('tbody tr')
      expect(rows.length).toBe(1)
      expect(rows[0].textContent).toContain('USDC')
      await unmount(container, root)
    })

    it('shows a "filtered of total" count', async () => {
      const { container, root } = render(<AllocationList items={ITEMS} />)
      expect(container.textContent).toContain('2 of 2')
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
      const many: AllocationItem[] = Array.from({ length: 30 }, (_, i) =>
        makeItem({ asset_id: `a-${i}`, symbol: `A${i}`, value_usd: '100.00', percentage: '3.33' }),
      )
      const { visible, hidden } = splitAllocations(many)
      expect(visible.length).toBe(12)
      expect(hidden.length).toBe(18)
    })

    it('keeps a five-row floor when every share is tiny', () => {
      const dust: AllocationItem[] = Array.from({ length: 20 }, (_, i) =>
        makeItem({ asset_id: `d-${i}`, symbol: `D${i}`, value_usd: '1.00', percentage: '0.05' }),
      )
      const { visible, hidden } = splitAllocations(dust)
      expect(visible.length).toBe(5)
      expect(hidden.length).toBe(15)
    })

    it('summarises hidden unpriced rows as zero value without throwing', async () => {
      const items = [
        ...dustyPortfolio(10),
        makeItem({ asset_id: 'xyz', symbol: 'XYZ', value_usd: null, percentage: '0' }),
      ]
      const { container, root } = render(<AllocationList items={items} />)
      const toggle = container.querySelector('.allocation-spoiler')!
      expect(toggle.textContent).toContain('8 smaller assets')
      await unmount(container, root)
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
      expect(container.querySelectorAll('th[scope="col"]').length).toBeGreaterThanOrEqual(4)
      await unmount(container, root)
    })

    it('percentage cells have aria-label with "percent" text', async () => {
      const { container, root } = render(<AllocationList items={ITEMS} />)
      expect(container.querySelectorAll('span[aria-label*="percent"]').length).toBe(2)
      await unmount(container, root)
    })
  })
})
