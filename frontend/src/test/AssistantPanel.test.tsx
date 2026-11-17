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
}))

const chat = vi.mocked(client.assistantChat)

const OK = {
  reply: 'Summary\n\n- point one\n- point two',
  mode: 'portfolio' as const,
  provider: 'openai_compatible',
  model: 'deepseek-v4',
  context_as_of: '2026-11-17T10:00:00Z',
  usage: { input_tokens: 1, output_tokens: 1 },
  elapsed_ms: 4200,
  conversation_id: '00000000-0000-0000-0000-000000000001',
  message_id: '00000000-0000-0000-0000-000000000002',
}

function apiError(status: number, code: string, message: string) {
  return new ApiError(status, { error: { code, message }, request_id: 'r' })
}

describe('AssistantPanel', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    chat.mockReset()
  })

  afterEach(async () => {
    await act(async () => root.unmount())
    document.body.removeChild(container)
    vi.useRealTimers()
  })

  function mount() {
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    act(() => {
      root.render(
        React.createElement(QueryClientProvider, { client: qc }, React.createElement(AssistantPanel)),
      )
    })
  }
  const q = (id: string) => container.querySelector(`[data-testid="${id}"]`) as HTMLElement | null
  async function click(id: string) {
    await act(async () => q(id)!.click())
  }

  it('"Analyse portfolio" sends mode portfolio with empty messages and shows the answer + caption', async () => {
    chat.mockResolvedValue(OK)
    mount()
    await click('assistant-quick-portfolio')
    expect(chat).toHaveBeenCalledWith({ mode: 'portfolio', messages: [] })
    expect(q('assistant-message')!.textContent).toContain('- point one')
    expect(q('assistant-caption')!.textContent).toContain('deepseek-v4')
    expect(q('assistant-caption')!.textContent).toContain('2026-11-17T10:00:00Z')
    expect(q('assistant-caption')!.textContent).toContain('4.2 s')
    expect(q('assistant-disclaimer')!.textContent).toContain('not financial advice')
  })

  it('"What matters in the news" sends mode news', async () => {
    chat.mockResolvedValue({ ...OK, mode: 'news' })
    mount()
    await click('assistant-quick-news')
    expect(chat).toHaveBeenCalledWith({ mode: 'news', messages: [] })
  })

  it('free-form question sends mode chat with the history', async () => {
    chat.mockResolvedValue(OK)
    mount()
    const input = q('assistant-input') as HTMLInputElement
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
    await act(async () => {
      setter?.call(input, 'Why is ETH down?')
      input.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await act(async () => {
      container.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    })
    expect(chat).toHaveBeenCalledWith({
      mode: 'chat',
      messages: [{ role: 'user', content: 'Why is ETH down?' }],
    })
  })

  it('shows a live "Analysing… N s" indicator and disables sending while in flight', async () => {
    vi.useFakeTimers()
    chat.mockReturnValue(new Promise(() => {}))
    mount()
    await click('assistant-quick-portfolio')
    expect(q('assistant-pending')!.textContent).toContain('Analysing… 0 s')
    await act(async () => {
      vi.advanceTimersByTime(3000)
    })
    expect(q('assistant-pending')!.textContent).toContain('Analysing… 3 s')
    expect((q('assistant-quick-portfolio') as HTMLButtonElement).disabled).toBe(true)
    expect((q('assistant-quick-news') as HTMLButtonElement).disabled).toBe(true)
    expect((q('assistant-input') as HTMLInputElement).disabled).toBe(true)
  })

  it('409 llm_not_configured shows the empty state with a Connections link', async () => {
    chat.mockRejectedValue(apiError(409, 'llm_not_configured', 'no llm'))
    mount()
    await click('assistant-quick-portfolio')
    expect(q('assistant-not-configured')!.textContent).toContain('LLM is not connected')
    expect(q('assistant-not-configured')!.querySelector('a')!.getAttribute('href')).toBe('#/connections')
    expect(q('assistant-error')).toBeNull()
  })

  it('502 shows the server message and Retry re-sends the same request', async () => {
    chat.mockRejectedValueOnce(apiError(502, 'llm_unavailable', 'provider is down'))
    chat.mockResolvedValueOnce(OK)
    mount()
    await click('assistant-quick-news')
    expect(q('assistant-error')!.textContent).toContain('provider is down')
    await click('assistant-retry')
    expect(chat).toHaveBeenCalledTimes(2)
    expect(chat).toHaveBeenLastCalledWith({ mode: 'news', messages: [] })
    expect(q('assistant-error')).toBeNull()
    expect(q('assistant-message')).toBeTruthy()
  })
})
