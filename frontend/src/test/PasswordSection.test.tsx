import { describe, it, expect, vi, afterEach } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../api/client', async () => {
  const { MockApiError } = await import('./mockApiError')
  return {
    changePassword: vi.fn(),
    ApiError: MockApiError,
  }
})

import PasswordSection from '../components/accountData/PasswordSection'
import { changePassword } from '../api/client'

const mockChangePassword = vi.mocked(changePassword)

function mountComponent() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  })
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  act(() => {
    root.render(
      React.createElement(QueryClientProvider, { client: qc }, React.createElement(PasswordSection)),
    )
  })
  return { container, root }
}

// Setting `.value` directly leaves React's internal value tracker believing
// nothing changed, so the subsequent 'input' event never reaches onChange.
// Go through the native setter, same trick `fireEvent.change` uses.
const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
  window.HTMLInputElement.prototype,
  'value',
)!.set!

function setValue(input: HTMLInputElement, value: string) {
  nativeInputValueSetter.call(input, value)
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

function submitForm(container: HTMLElement) {
  const form = container.querySelector('form[aria-label="Change password form"]') as HTMLFormElement
  form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
}

describe('PasswordSection', () => {
  let container: HTMLDivElement
  let root: Root

  afterEach(async () => {
    await act(async () => {
      root.unmount()
    })
    document.body.removeChild(container)
    vi.clearAllMocks()
  })

  it('rejects a new password shorter than 12 characters without calling the API', async () => {
    ;({ container, root } = mountComponent())

    const current = container.querySelector('#current-password') as HTMLInputElement
    const next = container.querySelector('#new-password') as HTMLInputElement
    const confirm = container.querySelector('#confirm-password') as HTMLInputElement

    await act(async () => {
      setValue(current, 'old-password-123')
      setValue(next, 'short')
      setValue(confirm, 'short')
    })
    await act(async () => { submitForm(container) })

    expect(container.textContent).toContain('New password must be at least 12 characters.')
    expect(mockChangePassword).not.toHaveBeenCalled()
  })

  it('rejects a mismatched confirmation without calling the API', async () => {
    ;({ container, root } = mountComponent())

    const current = container.querySelector('#current-password') as HTMLInputElement
    const next = container.querySelector('#new-password') as HTMLInputElement
    const confirm = container.querySelector('#confirm-password') as HTMLInputElement

    await act(async () => {
      setValue(current, 'old-password-123')
      setValue(next, 'new-password-1234')
      setValue(confirm, 'new-password-9999')
    })
    await act(async () => { submitForm(container) })

    expect(container.textContent).toContain('New password and confirmation do not match.')
    expect(mockChangePassword).not.toHaveBeenCalled()
  })

  it('submits the current and new password and shows the sign-out notice on success', async () => {
    mockChangePassword.mockResolvedValue(undefined)
    ;({ container, root } = mountComponent())

    const current = container.querySelector('#current-password') as HTMLInputElement
    const next = container.querySelector('#new-password') as HTMLInputElement
    const confirm = container.querySelector('#confirm-password') as HTMLInputElement

    await act(async () => {
      setValue(current, 'old-password-123')
      setValue(next, 'new-password-1234')
      setValue(confirm, 'new-password-1234')
    })
    await act(async () => { submitForm(container) })

    expect(mockChangePassword).toHaveBeenCalledWith('old-password-123', 'new-password-1234')
    await vi.waitFor(() => expect(container.textContent).toContain('All other sessions have been signed out.'))
    expect(current.value).toBe('')
    expect(next.value).toBe('')
    expect(confirm.value).toBe('')
  })

  it('shows the API error message when the current password is wrong', async () => {
    const { MockApiError } = await import('./mockApiError')
    mockChangePassword.mockRejectedValue(new MockApiError(401, 'Current password is incorrect.'))
    ;({ container, root } = mountComponent())

    const current = container.querySelector('#current-password') as HTMLInputElement
    const next = container.querySelector('#new-password') as HTMLInputElement
    const confirm = container.querySelector('#confirm-password') as HTMLInputElement

    await act(async () => {
      setValue(current, 'wrong-password')
      setValue(next, 'new-password-1234')
      setValue(confirm, 'new-password-1234')
    })
    await act(async () => { submitForm(container) })

    await vi.waitFor(() => expect(container.textContent).toContain('Current password is incorrect.'))
  })

  it('falls back to a generic message for a non-API error', async () => {
    mockChangePassword.mockRejectedValue(new Error('network down'))
    ;({ container, root } = mountComponent())

    const current = container.querySelector('#current-password') as HTMLInputElement
    const next = container.querySelector('#new-password') as HTMLInputElement
    const confirm = container.querySelector('#confirm-password') as HTMLInputElement

    await act(async () => {
      setValue(current, 'old-password-123')
      setValue(next, 'new-password-1234')
      setValue(confirm, 'new-password-1234')
    })
    await act(async () => { submitForm(container) })

    await vi.waitFor(() => expect(container.textContent).toContain('Failed to change password.'))
  })
})
