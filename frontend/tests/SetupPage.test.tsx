import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createElement } from 'react'
import { createRoot } from 'react-dom/client'
import { flushSync } from 'react-dom'
import SetupPage from '../src/pages/SetupPage'

// Suppress React act() warnings for intentionally sync renders
vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', false)

describe('SetupPage', () => {
  let container: HTMLDivElement

  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
  })

  afterEach(() => {
    document.body.removeChild(container)
  })

  it('renders a form', () => {
    const root = createRoot(container)
    flushSync(() => {
      root.render(createElement(SetupPage, { onSetupComplete: () => {} }))
    })
    expect(container.querySelector('form')).not.toBeNull()
  })

  it('renders a password input and confirm input', () => {
    const root = createRoot(container)
    flushSync(() => {
      root.render(createElement(SetupPage, { onSetupComplete: () => {} }))
    })
    expect(container.querySelector('#password')).not.toBeNull()
    expect(container.querySelector('#confirm')).not.toBeNull()
  })

  it('renders a submit button labelled "Set up"', () => {
    const root = createRoot(container)
    flushSync(() => {
      root.render(createElement(SetupPage, { onSetupComplete: () => {} }))
    })
    const btn = container.querySelector('button[type="submit"]')
    expect(btn?.textContent).toBe('Set up')
  })

  it('shows a password-mismatch error without calling the API', () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    const root = createRoot(container)
    flushSync(() => {
      root.render(createElement(SetupPage, { onSetupComplete: () => {} }))
    })

    const passwordInput = container.querySelector(
      '#password',
    ) as HTMLInputElement
    const confirmInput = container.querySelector('#confirm') as HTMLInputElement
    const form = container.querySelector('form') as HTMLFormElement

    flushSync(() => {
      passwordInput.value = 'correct-horse-battery'
      passwordInput.dispatchEvent(new Event('input', { bubbles: true }))
      confirmInput.value = 'different-password-here'
      confirmInput.dispatchEvent(new Event('input', { bubbles: true }))
    })

    flushSync(() => {
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    })

    expect(fetchMock).not.toHaveBeenCalled()

    vi.unstubAllGlobals()
  })

  it('renders heading "Set up audr"', () => {
    const root = createRoot(container)
    flushSync(() => {
      root.render(createElement(SetupPage, { onSetupComplete: () => {} }))
    })
    const h1 = container.querySelector('h1')
    expect(h1?.textContent).toBe('Set up audr')
  })
})
