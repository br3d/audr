import { describe, it, expect } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import MoneyValue from '../components/MoneyValue'

// --- helpers ---

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

// --- MoneyValue unit tests (AUD-60, AUD-63) ---

describe('MoneyValue', () => {
  describe('null / unknown values', () => {
    it('renders default "unknown" label for null value', async () => {
      const { container, root } = render(<MoneyValue value={null} />)
      expect(container.querySelector('[data-testid="money-unknown"]')).toBeTruthy()
      expect(container.textContent).toBe('unknown')
      await unmount(container, root)
    })

    it('renders custom unknownLabel for null value', async () => {
      const { container, root } = render(
        <MoneyValue value={null} unknownLabel="unavailable" />,
      )
      expect(container.textContent).toBe('unavailable')
      await unmount(container, root)
    })

    it('does not render a dollar sign for null value', async () => {
      const { container, root } = render(<MoneyValue value={null} />)
      expect(container.textContent).not.toContain('$')
      await unmount(container, root)
    })
  })

  describe('display formatting', () => {
    it('formats zero as $0.00', async () => {
      const { container, root } = render(<MoneyValue value="0" />)
      expect(container.textContent).toBe('$0.00')
      await unmount(container, root)
    })

    it('formats a small integer string with two decimal places', async () => {
      const { container, root } = render(<MoneyValue value="1" />)
      expect(container.textContent).toBe('$1.00')
      await unmount(container, root)
    })

    it('formats a decimal with more than 2 places (truncates to 2)', async () => {
      const { container, root } = render(<MoneyValue value="1.5" />)
      expect(container.textContent).toBe('$1.50')
      await unmount(container, root)
    })

    it('rounds a value with many decimal places to 2', async () => {
      const { container, root } = render(<MoneyValue value="3.14159265" />)
      expect(container.textContent).toBe('$3.14')
      await unmount(container, root)
    })

    it('adds thousands separator for values >= 1000', async () => {
      const { container, root } = render(<MoneyValue value="1234.56" />)
      expect(container.textContent).toBe('$1,234.56')
      await unmount(container, root)
    })

    it('adds multiple thousands separators for large values', async () => {
      const { container, root } = render(<MoneyValue value="1234567.89" />)
      expect(container.textContent).toBe('$1,234,567.89')
      await unmount(container, root)
    })

    it('handles a very large value without floating-point distortion', async () => {
      // 9_007_199_254_740_992 is beyond JS Number precision but Decimal.js handles it
      const { container, root } = render(
        <MoneyValue value="9007199254740992.50" />,
      )
      expect(container.textContent).toBe('$9,007,199,254,740,992.50')
      await unmount(container, root)
    })

    it('renders negative values with a leading minus sign', async () => {
      const { container, root } = render(<MoneyValue value="-99.95" />)
      expect(container.textContent).toBe('-$99.95')
      await unmount(container, root)
    })

    it('renders data-testid="money-value" for a non-null value', async () => {
      const { container, root } = render(<MoneyValue value="42.00" />)
      expect(container.querySelector('[data-testid="money-value"]')).toBeTruthy()
      await unmount(container, root)
    })
  })

  describe('emphasizeInteger', () => {
    it('keeps a single flat string by default (no emphasis markup)', async () => {
      const { container, root } = render(<MoneyValue value="1234.56" />)
      expect(container.querySelector('.money-integer')).toBeNull()
      expect(container.querySelector('.money-fraction')).toBeNull()
      expect(container.textContent).toBe('$1,234.56')
      await unmount(container, root)
    })

    it('splits integer and fractional parts into separate spans when enabled', async () => {
      const { container, root } = render(<MoneyValue value="1234.56" emphasizeInteger />)
      const intEl = container.querySelector('.money-integer')
      const fracEl = container.querySelector('.money-fraction')
      expect(intEl?.textContent).toBe('$1,234')
      expect(fracEl?.textContent).toBe('.56')
      expect(container.textContent).toBe('$1,234.56')
      await unmount(container, root)
    })

    it('keeps the leading minus sign with the integer span for negative values', async () => {
      const { container, root } = render(<MoneyValue value="-99.95" emphasizeInteger />)
      expect(container.querySelector('.money-integer')?.textContent).toBe('-$99')
      expect(container.querySelector('.money-fraction')?.textContent).toBe('.95')
      await unmount(container, root)
    })

    it('still sets the full-value aria-label when emphasizing the integer', async () => {
      const { container, root } = render(<MoneyValue value="1234.56" emphasizeInteger />)
      const span = container.querySelector('[data-testid="money-value"]')!
      expect(span.getAttribute('aria-label')).toBe('USD amount: $1,234.56')
      await unmount(container, root)
    })
  })

  describe('accessibility', () => {
    it('sets aria-label with "USD amount:" prefix for a non-null value', async () => {
      const { container, root } = render(<MoneyValue value="1234.00" />)
      const span = container.querySelector('[data-testid="money-value"]')!
      expect(span.getAttribute('aria-label')).toBe('USD amount: $1,234.00')
      await unmount(container, root)
    })

    it('sets aria-label with "USD amount:" prefix for null value', async () => {
      const { container, root } = render(<MoneyValue value={null} />)
      const span = container.querySelector('[data-testid="money-unknown"]')!
      expect(span.getAttribute('aria-label')).toBe('USD amount: unknown')
      await unmount(container, root)
    })
  })
})
