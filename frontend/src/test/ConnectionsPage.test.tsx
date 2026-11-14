import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../api/client', async () => {
  const { MockApiError } = await import('./mockApiError')
  return {
    fetchIntegrations: vi.fn(),
    updateRpc: vi.fn(),
    updateQuotes: vi.fn(),
    updateLlm: vi.fn(),
    validateIntegration: vi.fn(),
    ApiError: MockApiError,
  }
})

import ConnectionsPage from '../pages/ConnectionsPage'
import { fetchIntegrations, updateQuotes, updateLlm } from '../api/client'
import type { IntegrationEntry, IntegrationsResponse, ProviderOption } from '../api/client'

const mockFetchIntegrations = vi.mocked(fetchIntegrations)
const mockUpdateQuotes = vi.mocked(updateQuotes)
const mockUpdateLlm = vi.mocked(updateLlm)

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

const LLM_OPTIONS: ProviderOption[] = [
  {
    id: 'anthropic',
    label: 'Anthropic (Claude)',
    requires_api_key: true,
    note: 'Native.',
    provider: 'anthropic',
    base_url: null,
    default_model: 'claude-haiku-4-5',
    private_host: false,
  },
  {
    id: 'openai',
    label: 'OpenAI',
    requires_api_key: true,
    note: 'Needs key.',
    provider: 'openai_compatible',
    base_url: 'https://api.openai.com/v1',
    default_model: 'gpt-4o-mini',
    private_host: false,
  },
  {
    id: 'ollama',
    label: 'Ollama (local)',
    requires_api_key: false,
    note: 'No key.',
    provider: 'openai_compatible',
    base_url: 'http://localhost:11434/v1',
    default_model: 'llama3.1',
    private_host: true,
  },
  {
    id: 'custom',
    label: 'Custom (OpenAI-compatible)',
    requires_api_key: false,
    note: 'Any.',
    provider: 'openai_compatible',
    base_url: null,
    default_model: null,
    private_host: false,
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
    {
      kind: 'llm',
      configured: false,
      enabled: true,
      provider: 'anthropic',
      host_label: null,
      revision: '0',
      health,
      effective_source: null,
      using_default: false,
      options: LLM_OPTIONS,
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

  async function setValue(el: HTMLInputElement | HTMLSelectElement, v: string) {
    await act(async () => {
      const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype
      Object.getOwnPropertyDescriptor(proto, 'value')?.set?.call(el, v)
      el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }))
    })
  }
  const q = (id: string) => container.querySelector(`#${id}`) as HTMLInputElement

  async function openLlm() {
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(container.textContent).toContain('In use:'))
    await act(async () => {
      buttonByText(container, 'Set up LLM provider').click()
    })
  }

  it('shows the Anthropic preset with a masked key field and no base URL', async () => {
    await openLlm()
    expect((q('llm-preset') as unknown as HTMLSelectElement).value).toBe('anthropic')
    expect(q('llm-api-key').type).toBe('password')
    expect(container.querySelector('#llm-base-url')).toBeNull()
    expect(container.textContent).toContain('Any OpenAI-compatible provider works')
  })

  it('pre-fills the base URL from a preset and keeps Custom editable', async () => {
    await openLlm()
    await setValue(q('llm-preset'), 'openai')
    expect(q('llm-base-url').value).toBe('https://api.openai.com/v1')
    expect(q('llm-model').placeholder).toBe('gpt-4o-mini')
    await setValue(q('llm-preset'), 'custom')
    expect(q('llm-base-url').value).toBe('https://api.openai.com/v1')
    await setValue(q('llm-base-url'), 'https://my.host/v1')
    expect(q('llm-base-url').value).toBe('https://my.host/v1')
  })

  it('requires a key for a keyed provider with none stored', async () => {
    await openLlm()
    await act(async () => {
      buttonByText(container, 'Save Anthropic').click()
    })
    expect(mockUpdateLlm).not.toHaveBeenCalled()
    expect(container.textContent).toContain('An API key is required')
  })

  it('saves Anthropic with the key, and never echoes it into the DOM', async () => {
    mockUpdateLlm.mockResolvedValue({ ...defaultEntries()[2], configured: true })
    await openLlm()
    await setValue(q('llm-api-key'), 'sk-ant-secret-value')
    await act(async () => {
      buttonByText(container, 'Save Anthropic').click()
    })
    expect(mockUpdateLlm).toHaveBeenCalledWith({
      revision: '0',
      provider: 'anthropic',
      api_key: 'sk-ant-secret-value',
    })
    expect(container.innerHTML).not.toContain('sk-ant-secret-value')
  })

  it('saves Ollama with no key and flags the private host', async () => {
    mockUpdateLlm.mockResolvedValue({ ...defaultEntries()[2], configured: true })
    await openLlm()
    await setValue(q('llm-preset'), 'ollama')
    await act(async () => {
      buttonByText(container, 'Save Ollama').click()
    })
    expect(mockUpdateLlm).toHaveBeenCalledWith({
      revision: '0',
      provider: 'openai_compatible',
      base_url: 'http://localhost:11434/v1',
      allow_private_host: true,
    })
  })

  it('sends prices when entered', async () => {
    mockUpdateLlm.mockResolvedValue({ ...defaultEntries()[2], configured: true })
    await openLlm()
    await setValue(q('llm-preset'), 'ollama')
    await setValue(q('llm-model'), 'qwen3')
    await setValue(q('llm-price-in'), '0')
    await setValue(q('llm-price-out'), '0')
    await act(async () => {
      buttonByText(container, 'Save Ollama').click()
    })
    expect(mockUpdateLlm).toHaveBeenCalledWith(
      expect.objectContaining({ model: 'qwen3', price_input_per_mtok: '0', price_output_per_mtok: '0' }),
    )
  })

  it('renders a stored Anthropic setup as the Anthropic preset and leaves a blank key unsent', async () => {
    mockUpdateLlm.mockResolvedValue({ ...defaultEntries()[2], configured: true })
    const items = defaultEntries()
    items[2] = { ...items[2], configured: true, revision: '3', model: 'claude-haiku-4-5' }
    mockFetchIntegrations.mockResolvedValue(makeResponse(items))
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(container.textContent).toContain('In use:'))
    await act(async () => {
      buttonByText(container, 'Change LLM provider').click()
    })
    expect((q('llm-preset') as unknown as HTMLSelectElement).value).toBe('anthropic')
    expect(q('llm-api-key').placeholder).toContain('saved')
    await act(async () => {
      buttonByText(container, 'Save Anthropic').click()
    })
    const call = mockUpdateLlm.mock.calls[0][0]
    expect(call).not.toHaveProperty('api_key')
    expect(call).toMatchObject({ revision: '3', provider: 'anthropic', model: 'claude-haiku-4-5' })
  })
})
