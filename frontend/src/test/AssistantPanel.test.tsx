import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import React, { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import AssistantPanel from '../components/AssistantPanel'
import { ApiError } from '../api/client'
import * as client from '../api/client'

vi.mock('../api/client', async (orig) => ({
  ...(await orig<typeof import('../api/client')>()),
  assistantChat: vi.fn(),
  fetchAssistantPresets: vi.fn(),
  fetchPortfolio: vi.fn(),
  listAssistantConversations: vi.fn(),
  getAssistantConversation: vi.fn(),
  renameAssistantConversation: vi.fn(),
  deleteAssistantConversation: vi.fn(),
  deleteAllAssistantConversations: vi.fn(),
}))

const chat = vi.mocked(client.assistantChat)
const presetsApi = vi.mocked(client.fetchAssistantPresets)
const portfolioApi = vi.mocked(client.fetchPortfolio)
const PRESETS = [
  { id: 'daily_brief', label: 'Daily brief', description: 'Short portfolio brief', needs_asset: false },
  { id: 'news_impact', label: 'News impact', description: 'What news matters', needs_asset: false },
  { id: 'asset_deep_dive', label: 'Asset deep dive', description: 'One asset in depth', needs_asset: true },
]
const alloc = (id: string, symbol: string) => ({
  asset_id: id,
  symbol,
  name: symbol + ' coin',
  included: true,
})
const list = vi.mocked(client.listAssistantConversations)
const getConv = vi.mocked(client.getAssistantConversation)
const rename = vi.mocked(client.renameAssistantConversation)
const del = vi.mocked(client.deleteAssistantConversation)
const delAll = vi.mocked(client.deleteAllAssistantConversations)

const C1 = '00000000-0000-0000-0000-000000000001'
const C2 = '00000000-0000-0000-0000-000000000003'
const M = '00000000-0000-0000-0000-000000000002'

const OK = {
  reply: 'Summary\n\n- point one\n- point two',
  mode: 'portfolio' as const,
  provider: 'openai_compatible',
  model: 'deepseek-v4',
  context_as_of: '2026-11-17T10:00:00Z',
  usage: { input_tokens: 1, output_tokens: 1 },
  elapsed_ms: 4200,
  conversation_id: C1,
  message_id: M,
}

function summary(id: string, title: string, mode: 'portfolio' | 'news' | 'chat', date: string) {
  return { id, title, mode, created_at: date, updated_at: date, message_count: 2 }
}
const S1 = summary(C1, 'Portfolio analysis', 'portfolio', '2026-11-17T09:00:00Z')
const S2 = summary(C2, 'Why is ETH down?', 'chat', '2026-11-16T09:00:00Z')

function stored(over: Partial<client.AssistantStoredMessage>): client.AssistantStoredMessage {
  return {
    id: M,
    role: 'assistant',
    content: '',
    status: 'done',
    created_at: '2026-11-17T09:00:00Z',
    ...over,
  }
}
const USER = stored({ id: 'u1', role: 'user', content: 'Analyse my portfolio.' })
const ANSWER = stored({
  id: 'a1',
  content: 'Summary\n\n- point one',
  model: 'deepseek-v4',
  context_as_of: '2026-11-17T10:00:00Z',
  elapsed_ms: 4200,
})

function detailOf(s: typeof S1, messages: client.AssistantStoredMessage[]) {
  return { ...s, messages }
}

function apiError(status: number, code: string, message: string) {
  return new ApiError(status, { error: { code, message }, request_id: 'r' })
}

describe('AssistantPanel', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    window.history.replaceState(null, '', '#/assistant')
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    for (const m of [chat, presetsApi, portfolioApi, list, getConv, rename, del, delAll]) m.mockReset()
    presetsApi.mockResolvedValue(PRESETS)
    portfolioApi.mockResolvedValue({
      allocations: [alloc('a-eth', 'ETH'), alloc('a-usdc', 'USDC')],
    } as unknown as client.PortfolioResponse)
    list.mockResolvedValue({ items: [S1, S2], total: 2, limit: 100, offset: 0 })
    getConv.mockImplementation(async (id) =>
      id === C2
        ? detailOf(S2, [stored({ id: 'u2', role: 'user', content: 'Why is ETH down?' }), ANSWER])
        : detailOf(S1, [USER, ANSWER]),
    )
  })

  afterEach(async () => {
    await act(async () => root.unmount())
    document.body.removeChild(container)
    vi.useRealTimers()
  })

  async function mount() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    await act(async () => {
      root.render(
        React.createElement(QueryClientProvider, { client: qc }, React.createElement(AssistantPanel)),
      )
    })
    // Let the list/transcript queries resolve (and the transcript follow the list).
    await flush()
  }
  async function flush() {
    for (let i = 0; i < 4; i++) {
      await act(async () => {
        if (vi.isFakeTimers()) await vi.advanceTimersByTimeAsync(0)
        else await new Promise((r) => setTimeout(r, 0))
      })
    }
  }
  const q = (id: string) => container.querySelector(`[data-testid="${id}"]`) as HTMLElement | null
  const qa = (id: string) => Array.from(container.querySelectorAll(`[data-testid="${id}"]`)) as HTMLElement[]
  async function click(el: HTMLElement | null) {
    await act(async () => el!.click())
    await flush()
  }
  async function type(text: string) {
    const input = q('assistant-input') as HTMLInputElement
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
    await act(async () => {
      setter?.call(input, text)
      input.dispatchEvent(new Event('input', { bubbles: true }))
    })
  }
  async function submit() {
    await act(async () => {
      container
        .querySelector('form[aria-label="Send message"]')!
        .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    })
  }

  it('renders the conversation list with title, date and mode badge', async () => {
    await mount()
    const rows = qa('assistant-conversation')
    expect(rows).toHaveLength(2)
    expect(rows[0].textContent).toContain('Portfolio analysis')
    expect(rows[0].textContent).toContain('2026-11-17')
    expect(rows[0].querySelector('[data-testid="assistant-mode-badge"]')!.textContent).toBe('portfolio')
    expect(rows[1].querySelector('[data-testid="assistant-mode-badge"]')!.textContent).toBe('chat')
  })

  it('selecting a conversation loads its transcript with the caption and puts the id in the hash', async () => {
    await mount()
    await click(qa('assistant-conversation-open')[1])
    expect(getConv).toHaveBeenCalledWith(C2)
    expect(window.location.hash).toBe(`#/assistant/${C2}`)
    expect(q('assistant-message')!.textContent).toContain('- point one')
    expect(q('assistant-caption')!.textContent).toContain('deepseek-v4')
    expect(q('assistant-caption')!.textContent).toContain('2026-11-17T10:00:00Z')
    expect(q('assistant-caption')!.textContent).toContain('4.2 s')
  })

  it('a reload with #/assistant/<id> opens that conversation', async () => {
    window.history.replaceState(null, '', `#/assistant/${C1}`)
    await mount()
    expect(getConv).toHaveBeenCalledWith(C1)
    expect(q('user-message')!.textContent).toContain('Analyse my portfolio.')
  })

  it('a continuation sends conversation_id and only the new user turn', async () => {
    window.history.replaceState(null, '', `#/assistant/${C2}`)
    chat.mockResolvedValue({ ...OK, mode: 'chat', conversation_id: C2 })
    await mount()
    await type('And BTC?')
    await submit()
    expect(chat).toHaveBeenCalledWith({
      mode: 'chat',
      messages: [{ role: 'user', content: 'And BTC?' }],
      conversation_id: C2,
    })
    // The transcript and list are re-fetched after the answer.
    expect(getConv.mock.calls.length).toBeGreaterThan(1)
  })

  it('a free-form question without a selection starts a new conversation and selects it', async () => {
    chat.mockResolvedValue({ ...OK, mode: 'chat', conversation_id: C2 })
    await mount()
    await type('Why is ETH down?')
    await submit()
    expect(chat).toHaveBeenCalledWith({
      mode: 'chat',
      messages: [{ role: 'user', content: 'Why is ETH down?' }],
    })
    expect(window.location.hash).toBe(`#/assistant/${C2}`)
  })

  it('quick actions start a new conversation (no conversation_id), even with one open', async () => {
    window.history.replaceState(null, '', `#/assistant/${C2}`)
    chat.mockResolvedValue(OK)
    await mount()
    await click(q('assistant-preset-daily_brief'))
    expect(chat).toHaveBeenCalledWith({ preset: 'daily_brief', messages: [] })
    chat.mockResolvedValue({ ...OK, mode: 'news' })
    await click(q('assistant-preset-news_impact'))
    expect(chat).toHaveBeenLastCalledWith({ preset: 'news_impact', messages: [] })
  })

  it('renders one chip per preset from the API, with the description as tooltip', async () => {
    await mount()
    const chips = container.querySelectorAll('[data-testid^="assistant-preset-"]')
    expect(chips).toHaveLength(3)
    expect(q('assistant-preset-news_impact')!.textContent).toBe('News impact')
    expect(q('assistant-preset-news_impact')!.getAttribute('title')).toBe('What news matters')
  })

  it('asset presets wait for a picked asset, then send preset + asset_ref', async () => {
    chat.mockResolvedValue(OK)
    rename.mockResolvedValue({ ...S1, title: 'x' })
    await mount()
    await click(q('assistant-preset-asset_deep_dive'))
    expect(chat).not.toHaveBeenCalled()
    expect(qa('assistant-asset-option').map((b) => b.textContent)).toEqual(['ETH', 'USDC'])
    const search = q('assistant-asset-search') as HTMLInputElement
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
    await act(async () => {
      setter?.call(search, 'usd')
      search.dispatchEvent(new Event('input', { bubbles: true }))
    })
    expect(qa('assistant-asset-option').map((b) => b.textContent)).toEqual(['USDC'])
    await click(qa('assistant-asset-option')[0])
    expect(chat).toHaveBeenCalledWith({ preset: 'asset_deep_dive', asset_ref: 'a-usdc', messages: [] })
    expect(rename).toHaveBeenCalledWith(C1, 'Asset deep dive — USDC')
  })

  it('New chat clears the selection', async () => {
    window.history.replaceState(null, '', `#/assistant/${C1}`)
    await mount()
    expect(q('assistant-message')).toBeTruthy()
    await click(q('assistant-new-chat'))
    expect(window.location.hash).toBe('#/assistant')
    expect(q('assistant-message')).toBeNull()
  })

  it('shows a live "Analysing… N s" indicator and disables sending while in flight', async () => {
    chat.mockReturnValue(new Promise(() => {}))
    await mount()
    vi.useFakeTimers()
    await click(q('assistant-preset-daily_brief'))
    expect(q('assistant-pending')!.textContent).toContain('Analysing… 0 s')
    await act(async () => {
      vi.advanceTimersByTime(3000)
    })
    expect(q('assistant-pending')!.textContent).toContain('Analysing… 3 s')
    expect((q('assistant-preset-daily_brief') as HTMLButtonElement).disabled).toBe(true)
    expect((q('assistant-input') as HTMLInputElement).disabled).toBe(true)
  })

  it('renames a conversation', async () => {
    rename.mockResolvedValue({ ...S1, title: 'Renamed' })
    await mount()
    await click(qa('assistant-rename')[0])
    await type('') // no-op on the main input; keep focus semantics simple
    const input = q('assistant-rename-input') as HTMLInputElement
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
    await act(async () => {
      setter?.call(input, 'Renamed')
      input.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await act(async () => {
      input.closest('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    })
    expect(rename).toHaveBeenCalledWith(C1, 'Renamed')
  })

  it('deletes one conversation and leaves the selection if it was open', async () => {
    window.history.replaceState(null, '', `#/assistant/${C1}`)
    del.mockResolvedValue(undefined)
    await mount()
    await click(qa('assistant-delete')[0])
    expect(del).toHaveBeenCalledWith(C1)
    expect(window.location.hash).toBe('#/assistant')
  })

  it('Delete all history asks first and only deletes after the confirm', async () => {
    delAll.mockResolvedValue(undefined)
    await mount()
    await click(q('assistant-delete-all'))
    expect(delAll).not.toHaveBeenCalled()
    expect(q('assistant-confirm-all')).toBeTruthy()
    await click(q('assistant-confirm-all-yes'))
    expect(delAll).toHaveBeenCalledTimes(1)
  })

  it('Cancel on the delete-all confirm deletes nothing', async () => {
    await mount()
    await click(q('assistant-delete-all'))
    const cancel = Array.from(q('assistant-confirm-all')!.querySelectorAll('button')).find(
      (b) => b.textContent === 'Cancel',
    )!
    await click(cancel)
    expect(delAll).not.toHaveBeenCalled()
    expect(q('assistant-confirm-all')).toBeNull()
  })

  it('a pending message shows "Analysing…" and is re-fetched until it is done', async () => {
    window.history.replaceState(null, '', `#/assistant/${C1}`)
    const pending = stored({ id: 'a1', status: 'pending' })
    getConv.mockResolvedValueOnce(detailOf(S1, [USER, pending]))
    getConv.mockResolvedValue(detailOf(S1, [USER, ANSWER]))
    vi.useFakeTimers()
    await mount()
    expect(q('assistant-pending')!.textContent).toBe('Analysing…')
    expect(q('assistant-message')).toBeNull()
    expect((q('assistant-input') as HTMLInputElement).disabled).toBe(true)
    await act(async () => {
      vi.advanceTimersByTime(3100)
    })
    await flush()
    expect(getConv).toHaveBeenCalledTimes(2)
    expect(q('assistant-pending')).toBeNull()
    expect(q('assistant-message')!.textContent).toContain('- point one')
    // Settled: no more polling.
    await act(async () => {
      vi.advanceTimersByTime(10000)
    })
    expect(getConv).toHaveBeenCalledTimes(2)
  })

  it('an error message shows the error box and Retry re-sends the last user turn', async () => {
    window.history.replaceState(null, '', `#/assistant/${C1}`)
    getConv.mockResolvedValue(
      detailOf(S1, [
        USER,
        stored({ id: 'a1', status: 'error', content: 'provider is down', error_code: 'llm_unavailable' }),
      ]),
    )
    chat.mockResolvedValue(OK)
    await mount()
    expect(q('assistant-error')!.textContent).toContain('provider is down')
    await click(q('assistant-retry'))
    expect(chat).toHaveBeenCalledWith({
      mode: 'portfolio',
      messages: [{ role: 'user', content: 'Analyse my portfolio.' }],
      conversation_id: C1,
    })
  })

  it('copies an assistant answer to the clipboard', async () => {
    window.history.replaceState(null, '', `#/assistant/${C1}`)
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    await mount()
    await click(q('assistant-copy'))
    expect(writeText).toHaveBeenCalledWith('Summary\n\n- point one')
    expect(q('assistant-copy')!.textContent).toBe('Copied')
  })

  it('renders answers as plain text, never HTML', async () => {
    window.history.replaceState(null, '', `#/assistant/${C1}`)
    getConv.mockResolvedValue(
      detailOf(S1, [USER, stored({ id: 'a1', content: '<img src=x onerror=alert(1)>', model: 'm' })]),
    )
    await mount()
    expect(container.querySelector('.assistant-text img')).toBeNull()
    expect(q('assistant-message')!.textContent).toContain('<img src=x')
  })

  it('409 llm_not_configured shows the empty state with a Connections link', async () => {
    chat.mockRejectedValue(apiError(409, 'llm_not_configured', 'no llm'))
    await mount()
    await click(q('assistant-preset-daily_brief'))
    expect(q('assistant-not-configured')!.textContent).toContain('LLM is not connected')
    expect(q('assistant-not-configured')!.querySelector('a')!.getAttribute('href')).toBe('#/connections')
    expect(q('assistant-send-error')).toBeNull()
  })

  it('a failed send shows the server message and Retry re-sends the same request', async () => {
    chat.mockRejectedValueOnce(apiError(502, 'llm_unavailable', 'provider is down'))
    chat.mockResolvedValueOnce(OK)
    await mount()
    await click(q('assistant-preset-news_impact'))
    expect(q('assistant-send-error')!.textContent).toContain('provider is down')
    await click(q('assistant-send-retry'))
    expect(chat).toHaveBeenCalledTimes(2)
    expect(chat).toHaveBeenLastCalledWith({ preset: 'news_impact', messages: [] })
    expect(q('assistant-send-error')).toBeNull()
  })
})
