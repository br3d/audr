import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createElement } from 'react'
import { createRoot } from 'react-dom/client'
import { flushSync } from 'react-dom'
import { ScheduleCard } from '../src/pages/SchedulesPage'

vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', false)

// React 19 controlled inputs require the native HTMLInputElement setter to
// trigger onChange from tests — direct assignment bypasses React's value tracker.
const nativeInputSetter = Object.getOwnPropertyDescriptor(
  window.HTMLInputElement.prototype,
  'value',
)!.set!

function setInputValue(input: HTMLInputElement, value: string) {
  nativeInputSetter.call(input, value)
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

describe('ScheduleCard interval input', () => {
  let container: HTMLDivElement

  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
  })

  afterEach(() => {
    document.body.removeChild(container)
  })

  function renderCard(onChange: (s: number, en: boolean) => void, intervalSeconds = 300) {
    const root = createRoot(container)
    flushSync(() => {
      root.render(
        createElement(ScheduleCard, {
          id: 'quotes',
          label: 'Price quotes',
          kind: 'quotes' as const,
          intervalSeconds,
          enabled: true,
          onChange,
          disabled: false,
        }),
      )
    })
    return root
  }

  function getInput() {
    return container.querySelector('#quotes-interval') as HTMLInputElement
  }

  it('calls onChange with the typed value on blur', () => {
    const onChange = vi.fn()
    renderCard(onChange)
    const input = getInput()

    flushSync(() => {
      input.dispatchEvent(new FocusEvent('focusin', { bubbles: true }))
    })
    flushSync(() => {
      setInputValue(input, '600')
    })
    flushSync(() => {
      input.dispatchEvent(new FocusEvent('focusout', { bubbles: true }))
    })

    expect(onChange).toHaveBeenCalledWith(600, true)
  })

  it('calls onChange with the typed value on Enter', () => {
    const onChange = vi.fn()
    renderCard(onChange)
    const input = getInput()

    flushSync(() => {
      input.dispatchEvent(new FocusEvent('focusin', { bubbles: true }))
    })
    flushSync(() => {
      setInputValue(input, '3600')
    })
    flushSync(() => {
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    })

    expect(onChange).toHaveBeenCalledWith(3600, true)
  })

  it('restores a valid value when field is cleared then blurred', () => {
    const onChange = vi.fn()
    renderCard(onChange)
    const input = getInput()

    flushSync(() => {
      input.dispatchEvent(new FocusEvent('focusin', { bubbles: true }))
    })
    flushSync(() => {
      setInputValue(input, '')
    })
    flushSync(() => {
      input.dispatchEvent(new FocusEvent('focusout', { bubbles: true }))
    })

    // Empty input (NaN) → reverts to the committed intervalSeconds value (300)
    expect(onChange).toHaveBeenCalledWith(300, true)
  })

  it('clamps a value below min=30 to 30 on blur', () => {
    const onChange = vi.fn()
    renderCard(onChange)
    const input = getInput()

    flushSync(() => {
      input.dispatchEvent(new FocusEvent('focusin', { bubbles: true }))
    })
    flushSync(() => {
      setInputValue(input, '5')
    })
    flushSync(() => {
      input.dispatchEvent(new FocusEvent('focusout', { bubbles: true }))
    })

    expect(onChange).toHaveBeenCalledWith(30, true)
  })

  it('allows intermediate typing state without calling onChange', () => {
    const onChange = vi.fn()
    renderCard(onChange)
    const input = getInput()

    flushSync(() => {
      input.dispatchEvent(new FocusEvent('focusin', { bubbles: true }))
    })
    // Type "6" (intermediate — on the way to "600")
    flushSync(() => {
      setInputValue(input, '6')
    })

    // The prop onChange must NOT have been called yet
    expect(onChange).not.toHaveBeenCalled()
  })
})
