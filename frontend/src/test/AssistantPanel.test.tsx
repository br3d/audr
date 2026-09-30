import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import AssistantPanel from '../components/AssistantPanel'

describe('AssistantPanel', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    vi.useFakeTimers()
  })

  afterEach(async () => {
    await act(async () => {
      root.unmount()
    })
    document.body.removeChild(container)
    vi.useRealTimers()
    vi.clearAllMocks()
  })

  function mount(props: Partial<React.ComponentProps<typeof AssistantPanel>> = {}) {
    act(() => {
      root.render(React.createElement(AssistantPanel, props))
    })
  }

  it('renders the panel with initial greeting', () => {
    mount()
    expect(container.querySelector('[data-testid="assistant-panel"]')).toBeTruthy()
    expect(container.querySelector('[data-testid="assistant-messages"]')).toBeTruthy()
    const msgs = container.querySelectorAll('[data-testid="assistant-message"]')
    expect(msgs.length).toBe(1)
    expect(msgs[0].textContent).toContain('demo mode')
  })

  it('renders send input and button', () => {
    mount()
    expect(container.querySelector('[data-testid="assistant-input"]')).toBeTruthy()
    expect(container.querySelector('[data-testid="assistant-send"]')).toBeTruthy()
  })

  it('send button is disabled when input is empty', () => {
    mount()
    const sendBtn = container.querySelector('[data-testid="assistant-send"]') as HTMLButtonElement
    expect(sendBtn.disabled).toBe(true)
  })

  it('appends user message and stub reply after submit', async () => {
    mount()

    const input = container.querySelector('[data-testid="assistant-input"]') as HTMLInputElement
    const form = container.querySelector('form[aria-label="Send message"]') as HTMLFormElement

    // Use native setter so React's synthetic onChange fires in jsdom
    const nativeSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
    await act(async () => {
      nativeSetter?.call(input, 'What is my portfolio worth?')
      input.dispatchEvent(new Event('input', { bubbles: true }))
    })

    await act(async () => {
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    })

    const userMsgs = container.querySelectorAll('[data-testid="user-message"]')
    expect(userMsgs.length).toBe(1)
    expect(userMsgs[0].textContent).toContain('What is my portfolio worth?')

    // Typing indicator visible
    expect(container.querySelector('[data-testid="typing-indicator"]')).toBeTruthy()

    // Advance fake timers to resolve stub response
    await act(async () => {
      vi.advanceTimersByTime(700)
    })

    // Typing indicator gone, assistant reply appeared
    expect(container.querySelector('[data-testid="typing-indicator"]')).toBeNull()
    const assistantMsgs = container.querySelectorAll('[data-testid="assistant-message"]')
    expect(assistantMsgs.length).toBe(2)
  })

  it('calls onClose when Close button is clicked', async () => {
    const onClose = vi.fn()
    mount({ onClose })
    const closeBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.getAttribute('aria-label') === 'Close assistant panel',
    )
    expect(closeBtn).toBeTruthy()
    await act(async () => {
      closeBtn!.click()
    })
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('does not render Close button when onClose is not provided', () => {
    mount()
    const closeBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.getAttribute('aria-label') === 'Close assistant panel',
    )
    expect(closeBtn).toBeUndefined()
  })

  it('shows demo badge in heading', () => {
    mount()
    const heading = container.querySelector('h2')
    expect(heading?.textContent).toContain('demo')
  })
})
