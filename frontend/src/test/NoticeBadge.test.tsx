import { describe, it, expect, afterEach } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'

import NoticeBadge from '../components/NoticeBadge'

// AUD-686: the badge replaces permanent warning banners, so the contract is
// "nothing rendered until the user asks for it, by hover, keyboard or tap".
let mounted: { container: HTMLDivElement; root: Root } | null = null

function mount(notices: React.ReactNode[]) {
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  act(() => {
    root.render(React.createElement(NoticeBadge, { label: 'Chart data quality', notices }))
  })
  mounted = { container, root }
  return container
}

afterEach(() => {
  if (!mounted) return
  const { container, root } = mounted
  act(() => root.unmount())
  container.remove()
  mounted = null
})

describe('NoticeBadge', () => {
  it('renders nothing when there are no notices', () => {
    const container = mount([])
    expect(container.innerHTML).toBe('')
  })

  it('keeps the notice text out of the DOM until the trigger is hovered', () => {
    const container = mount(['Gap in this range.'])
    expect(container.querySelectorAll('.notice-item')).toHaveLength(0)

    const wrap = container.querySelector('.notice-badge')!
    act(() => {
      wrap.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }))
    })
    expect(container.textContent).toContain('Gap in this range.')

    act(() => {
      wrap.dispatchEvent(new MouseEvent('mouseout', { bubbles: true }))
    })
    expect(container.querySelectorAll('.notice-item')).toHaveLength(0)
  })

  it('closes on Escape after a tap', () => {
    const container = mount(['Gap in this range.', 'Some points are incomplete.'])
    const trigger = container.querySelector<HTMLButtonElement>('.notice-badge-trigger')!
    expect(trigger.getAttribute('aria-label')).toContain('2 notices')

    act(() => trigger.click())
    expect(container.querySelectorAll('.notice-item')).toHaveLength(2)

    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    })
    expect(container.querySelectorAll('.notice-item')).toHaveLength(0)
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
  })
})
