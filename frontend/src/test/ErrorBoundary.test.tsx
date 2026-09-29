import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import React from 'react'
import type { ReactNode } from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import ErrorBoundary from '../components/ErrorBoundary'

function ThrowOnRender({ message }: { message: string }): ReactNode {
  throw new Error(message)
}

describe('ErrorBoundary', () => {
  let container: HTMLDivElement
  let root: Root
  const originalConsoleError = console.error

  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    // Suppress React error boundary noise in test output
    console.error = vi.fn()
  })

  afterEach(async () => {
    await act(async () => {
      root.unmount()
    })
    document.body.removeChild(container)
    console.error = originalConsoleError
  })

  it('renders children normally when there is no error', () => {
    act(() => {
      root.render(
        React.createElement(
          ErrorBoundary,
          null,
          React.createElement('div', { 'data-testid': 'child' }, 'Hello'),
        ),
      )
    })
    expect(container.querySelector('[data-testid="child"]')).toBeTruthy()
    expect(container.textContent).toContain('Hello')
  })

  it('shows fallback UI with reload button when a child throws', () => {
    act(() => {
      root.render(
        React.createElement(
          ErrorBoundary,
          null,
          React.createElement(ThrowOnRender, { message: 'boom' }),
        ),
      )
    })
    expect(container.querySelector('[role="alert"]')).toBeTruthy()
    expect(container.textContent).toContain('Something went wrong')
    expect(container.textContent).toContain('boom')
    const reloadBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.includes('Reload'),
    )
    expect(reloadBtn).toBeTruthy()
  })

  it('does not show the fallback when children render without error', () => {
    act(() => {
      root.render(
        React.createElement(
          ErrorBoundary,
          null,
          React.createElement('span', null, 'OK'),
        ),
      )
    })
    expect(container.querySelector('[role="alert"]')).toBeNull()
  })
})
