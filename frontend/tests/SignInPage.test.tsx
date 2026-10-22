import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createElement } from 'react'
import { createRoot } from 'react-dom/client'
import { flushSync } from 'react-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SignInPage from '../src/pages/SignInPage'

vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', false)

function renderSignInPage(container: HTMLDivElement, onSignIn: () => void) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const root = createRoot(container)
  flushSync(() => {
    root.render(
      createElement(
        QueryClientProvider,
        { client: qc },
        createElement(SignInPage, { onSignIn }),
      ),
    )
  })
  return root
}

describe('SignInPage', () => {
  let container: HTMLDivElement

  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
  })

  afterEach(() => {
    document.body.removeChild(container)
  })

  it('renders a form', () => {
    renderSignInPage(container, () => {})
    expect(container.querySelector('form')).not.toBeNull()
  })

  it('renders a password input', () => {
    renderSignInPage(container, () => {})
    const input = container.querySelector('#password') as HTMLInputElement
    expect(input).not.toBeNull()
    expect(input.type).toBe('password')
  })

  it('renders a submit button labelled "Sign in"', () => {
    renderSignInPage(container, () => {})
    const btn = container.querySelector('button[type="submit"]')
    expect(btn?.textContent).toBe('Sign in')
  })

  it('renders heading "Sign in"', () => {
    renderSignInPage(container, () => {})
    const h1 = container.querySelector('h1')
    expect(h1?.textContent).toBe('Sign in')
  })

  it('calls the API on submit', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ csrf_token: 'tok' }),
    })
    vi.stubGlobal('fetch', fetchMock)

    const onSignIn = vi.fn()
    renderSignInPage(container, onSignIn)

    const input = container.querySelector('#password') as HTMLInputElement
    flushSync(() => {
      input.value = 'my-secret-password'
      input.dispatchEvent(new Event('input', { bubbles: true }))
    })

    await new Promise<void>((resolve) => {
      flushSync(() => {
        const form = container.querySelector('form') as HTMLFormElement
        form.dispatchEvent(
          new Event('submit', { bubbles: true, cancelable: true }),
        )
      })
      // Allow the async login() call to complete
      setTimeout(resolve, 50)
    })

    expect(fetchMock).toHaveBeenCalledOnce()

    vi.unstubAllGlobals()
  })
})
