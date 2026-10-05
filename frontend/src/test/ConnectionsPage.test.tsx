import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../api/client', () => {
  class ApiError extends Error {
    status: number
    body: undefined
    constructor(status: number, message: string) {
      super(message)
      this.name = 'ApiError'
      this.status = status
    }
  }
  return {
    fetchIntegrations: vi.fn(),
    updateRpc: vi.fn(),
    updateQuotes: vi.fn(),
    validateIntegration: vi.fn(),
    ApiError,
  }
})

import ConnectionsPage from '../pages/ConnectionsPage'
import { fetchIntegrations, updateQuotes } from '../api/client'
import type { IntegrationEntry, IntegrationsResponse, ProviderOption } from '../api/client'

const mockFetchIntegrations = vi.mocked(fetchIntegrations)
const mockUpdateQuotes = vi.mocked(updateQuotes)

const QUOTE_OPTIONS: ProviderOption[] = [
  {
    id: 'coinmarketcap',
    label: 'CoinMarketCap (public endpoints)',
    requires_api_key: false,
    note: 'Used by default and needs no account or API key.',
  },
  {
    id: 'coingecko',
    label: 'CoinGecko (Demo API)',
    requires_api_key: true,
    note: 'Needs a free CoinGecko Demo API key.',
  },
]

/** A fresh install: nothing configured, both keyless defaults in use. */
function defaultEntries(): IntegrationEntry[] {
  const health = { status: 'unvalidated' as const, last_checked_at: null, error_message: null }
  return [
    {
      kind: 'rpc',
      configured: false,
      enabled: true,
      provider: null,
      host_label: null,
      revision: '0',
      health,
      effective_source: 'ethereum-rpc.publicnode.com',
      using_default: true,
      options: [],
    },
    {
      kind: 'quotes',
      configured: false,
      enabled: true,
      provider: 'coinmarketcap',
      host_label: null,
      revision: '0',
      health,
      effective_source: 'CoinMarketCap (public endpoints)',
      using_default: true,
      options: QUOTE_OPTIONS,
    },
  ]
}

function makeResponse(items: IntegrationEntry[] = defaultEntries()): IntegrationsResponse {
  return { items, request_id: 'r1', generated_at: '2026-01-01T00:00:00Z' }
}

function mountPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  act(() => {
    root.render(
      React.createElement(QueryClientProvider, { client: qc }, React.createElement(ConnectionsPage)),
    )
  })
  return { container, root }
}

function buttonByText(container: HTMLElement, text: string): HTMLButtonElement {
  const match = Array.from(container.querySelectorAll('button')).find((b) =>
    (b.textContent ?? '').includes(text),
  )
  if (!match) throw new Error(`no button matching ${text}`)
  return match as HTMLButtonElement
}

describe('ConnectionsPage — the page must describe the source actually in use (AUD-440)', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    mockFetchIntegrations.mockResolvedValue(makeResponse())
  })

  afterEach(async () => {
    await act(async () => {
      root.unmount()
    })
    document.body.removeChild(container)
    vi.clearAllMocks()
  })

  it('names the keyless defaults instead of claiming values are unavailable', async () => {
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(container.textContent).toContain('In use:'))

    expect(container.textContent).toContain('ethereum-rpc.publicnode.com')
    expect(container.textContent).toContain('CoinMarketCap (public endpoints)')
    // The old copy promised the opposite of what the backend actually does.
    expect(container.textContent).not.toContain('values are unavailable')
  })

  it('states that the supported price providers are a fixed list', async () => {
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(container.textContent).toContain('In use:'))

    const text = container.textContent ?? ''
    expect(text).toContain('audr supports exactly these price providers')
    expect(text).toContain('CoinMarketCap (public endpoints), CoinGecko (Demo API)')
    expect(text).toContain('The list is fixed')
  })

  it('offers a change button per integration rather than an always-open form', async () => {
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(container.textContent).toContain('In use:'))

    expect(container.querySelector('#rpc-url')).toBeNull()
    expect(container.querySelector('#quotes-provider')).toBeNull()

    await act(async () => {
      buttonByText(container, 'Use my own RPC endpoint').click()
    })
    expect(container.querySelector('#rpc-url')).toBeTruthy()
  })

  it('picks the quote provider from the supported set, not free text', async () => {
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(container.textContent).toContain('In use:'))

    await act(async () => {
      buttonByText(container, 'Change quote provider').click()
    })

    const select = container.querySelector('#quotes-provider') as HTMLSelectElement
    expect(select.tagName).toBe('SELECT')
    expect(Array.from(select.options).map((o) => o.value)).toEqual(['coinmarketcap', 'coingecko'])
    expect(select.value).toBe('coinmarketcap')
    // The keyless default needs no key field, so none is shown.
    expect(container.querySelector('#quotes-api-key')).toBeNull()
    expect(container.textContent).toContain('needs no account or API key')
  })

  it('requires a key for the keyed provider and explains why', async () => {
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(container.textContent).toContain('In use:'))

    await act(async () => {
      buttonByText(container, 'Change quote provider').click()
    })

    const select = container.querySelector('#quotes-provider') as HTMLSelectElement
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set
      setter?.call(select, 'coingecko')
      select.dispatchEvent(new Event('change', { bubbles: true }))
    })

    expect(container.querySelector('#quotes-api-key')).toBeTruthy()
    expect(container.textContent).toContain('Needs a free CoinGecko Demo API key.')

    await act(async () => {
      buttonByText(container, 'Save quote provider').click()
    })
    expect(mockUpdateQuotes).not.toHaveBeenCalled()
    expect(container.textContent).toContain('requires an API key')
  })

  it('saves the keyless default with no api_key, which is what reverts the provider', async () => {
    mockUpdateQuotes.mockResolvedValue(defaultEntries()[1])
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(container.textContent).toContain('In use:'))

    await act(async () => {
      buttonByText(container, 'Change quote provider').click()
    })
    await act(async () => {
      buttonByText(container, 'Save quote provider').click()
    })

    expect(mockUpdateQuotes).toHaveBeenCalledWith({
      revision: '0',
      provider: 'coinmarketcap',
      api_key: undefined,
    })
  })
})
