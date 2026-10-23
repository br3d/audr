import { describe, it, expect, vi, afterEach } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../api/client', async () => {
  const { MockApiError } = await import('./mockApiError')
  return {
    fetchPurgePreview: vi.fn(),
    submitPurge: vi.fn(),
    ApiError: MockApiError,
  }
})

import PurgeSection from '../components/accountData/PurgeSection'
import { fetchPurgePreview, submitPurge } from '../api/client'

const mockFetchPurgePreview = vi.mocked(fetchPurgePreview)
const mockSubmitPurge = vi.mocked(submitPurge)

function makePreview(overrides: Partial<{
  provider: string
  quote_observation_count: number
  quote_set_count: number
  affected_valuation_count: number
}> = {}) {
  return {
    provider: 'coingecko-demo',
    quote_observation_count: 1200,
    quote_set_count: 40,
    affected_valuation_count: 7,
    ...overrides,
  }
}

function mountComponent() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  })
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  act(() => {
    root.render(
      React.createElement(QueryClientProvider, { client: qc }, React.createElement(PurgeSection)),
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

async function openPreview(container: HTMLElement) {
  const previewBtn = container.querySelector('button[aria-label="Preview purge impact"]') as HTMLButtonElement
  await act(async () => { previewBtn.click() })
}

describe('PurgeSection', () => {
  let container: HTMLDivElement
  let root: Root

  afterEach(async () => {
    await act(async () => {
      root.unmount()
    })
    document.body.removeChild(container)
    vi.clearAllMocks()
  })

  it('does not fetch a preview until Preview impact is clicked', async () => {
    ;({ container, root } = mountComponent())
    expect(mockFetchPurgePreview).not.toHaveBeenCalled()
  })

  it('loads and displays the purge preview counts', async () => {
    mockFetchPurgePreview.mockResolvedValue(makePreview())
    ;({ container, root } = mountComponent())

    await openPreview(container)
    await vi.waitFor(() => expect(container.textContent).toContain('1,200'))

    expect(mockFetchPurgePreview).toHaveBeenCalledWith('coingecko-demo')
    expect(container.textContent).toContain('40')
    expect(container.textContent).toContain('7')
  })

  it('shows an error when the preview fails to load', async () => {
    const { MockApiError } = await import('./mockApiError')
    mockFetchPurgePreview.mockRejectedValue(new MockApiError(500, 'Preview unavailable.'))
    ;({ container, root } = mountComponent())

    await openPreview(container)
    await vi.waitFor(() => expect(container.textContent).toContain('Preview unavailable.'))
  })

  it('hides the preview again when the provider is edited', async () => {
    mockFetchPurgePreview.mockResolvedValue(makePreview())
    ;({ container, root } = mountComponent())

    await openPreview(container)
    await vi.waitFor(() => expect(container.textContent).toContain('What will be deleted'))

    const providerInput = container.querySelector('#purge-provider') as HTMLInputElement
    await act(async () => { setValue(providerInput, 'other-provider') })

    expect(container.textContent).not.toContain('What will be deleted')
  })

  it('disables the confirm button until the checkbox is checked and a password is entered', async () => {
    mockFetchPurgePreview.mockResolvedValue(makePreview())
    ;({ container, root } = mountComponent())

    await openPreview(container)
    await vi.waitFor(() => expect(container.textContent).toContain('What will be deleted'))

    const confirmBtn = container.querySelector('button[aria-label="Confirm and submit purge"]') as HTMLButtonElement
    expect(confirmBtn.disabled).toBe(true)

    const checkbox = container.querySelector('#purge-confirm') as HTMLInputElement
    const password = container.querySelector('#purge-password') as HTMLInputElement
    await act(async () => { checkbox.click() })
    expect(confirmBtn.disabled).toBe(true)

    await act(async () => { setValue(password, 'owner-password-123') })
    expect(confirmBtn.disabled).toBe(false)
  })

  it('shows the API error when the confirmation password is wrong', async () => {
    const { MockApiError } = await import('./mockApiError')
    mockFetchPurgePreview.mockResolvedValue(makePreview())
    mockSubmitPurge.mockRejectedValue(new MockApiError(401, 'Current password is incorrect.'))
    ;({ container, root } = mountComponent())

    await openPreview(container)
    await vi.waitFor(() => expect(container.textContent).toContain('What will be deleted'))

    const checkbox = container.querySelector('#purge-confirm') as HTMLInputElement
    const password = container.querySelector('#purge-password') as HTMLInputElement
    await act(async () => {
      checkbox.click()
      setValue(password, 'wrong-password')
    })

    const form = container.querySelector('form[aria-label="Confirm purge"]') as HTMLFormElement
    await act(async () => { form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })) })

    expect(mockSubmitPurge).toHaveBeenCalledWith({
      provider: 'coingecko-demo',
      confirm: true,
      current_password: 'wrong-password',
    })
    await vi.waitFor(() => expect(container.textContent).toContain('Current password is incorrect.'))
    // Preview stays open so the owner can retry without re-fetching the impact.
    expect(container.textContent).toContain('What will be deleted')
  })

  it('submits the purge and shows the success message, collapsing the preview', async () => {
    mockFetchPurgePreview.mockResolvedValue(makePreview())
    mockSubmitPurge.mockResolvedValue({ run_id: 'r1', coalesced: false })
    ;({ container, root } = mountComponent())

    await openPreview(container)
    await vi.waitFor(() => expect(container.textContent).toContain('What will be deleted'))

    const checkbox = container.querySelector('#purge-confirm') as HTMLInputElement
    const password = container.querySelector('#purge-password') as HTMLInputElement
    await act(async () => {
      checkbox.click()
      setValue(password, 'owner-password-123')
    })

    const form = container.querySelector('form[aria-label="Confirm purge"]') as HTMLFormElement
    await act(async () => { form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })) })

    expect(mockSubmitPurge).toHaveBeenCalledWith({
      provider: 'coingecko-demo',
      confirm: true,
      current_password: 'owner-password-123',
    })
    expect(container.textContent).toContain('Provider data purge submitted for "coingecko-demo"')
    expect(container.textContent).not.toContain('What will be deleted')
  })
})
