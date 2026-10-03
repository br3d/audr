import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import {
  pageFromHash,
  hashForPage,
  parseQuery,
  serializeQuery,
  stringField,
  enumField,
  boolField,
  intField,
  useHashQueryState,
} from '../routing'

// ---- Pure function tests: serialization / parsing (AUD-353 AC4) ----

describe('pageFromHash / hashForPage with a query part', () => {
  it('parses the page slug and ignores a trailing query string', () => {
    expect(pageFromHash('#/history?period=30d')).toBe('history')
    expect(pageFromHash('#/wallets')).toBe('wallets')
  })

  it('returns null for an unknown page regardless of query', () => {
    expect(pageFromHash('#/not-a-page?x=1')).toBeNull()
  })

  it('returns null for the retired #/holdings page (AUD-405), falling back to the default', () => {
    expect(pageFromHash('#/holdings')).toBeNull()
  })

  it('builds a hash with or without a query string', () => {
    expect(hashForPage('history')).toBe('#/history')
    expect(hashForPage('history', 'period=30d')).toBe('#/history?period=30d')
    expect(hashForPage('history', '')).toBe('#/history')
  })
})

describe('field codecs', () => {
  it('stringField round-trips a non-default value and omits the default', () => {
    const f = stringField('')
    expect(f.parse(null)).toBe('')
    expect(f.parse('eth')).toBe('eth')
    expect(f.serialize('')).toBeNull()
    expect(f.serialize('eth')).toBe('eth')
  })

  it('enumField normalises anything outside the allowed set to the default', () => {
    const f = enumField(['24h', '7d', '30d', 'all'] as const, '7d')
    expect(f.parse('30d')).toBe('30d')
    expect(f.parse('bogus')).toBe('7d')
    expect(f.parse(null)).toBe('7d')
    expect(f.serialize('7d')).toBeNull()
    expect(f.serialize('30d')).toBe('30d')
  })

  it('boolField parses "1" as true and anything else as false', () => {
    const f = boolField(false)
    expect(f.parse('1')).toBe(true)
    expect(f.parse('0')).toBe(false)
    expect(f.parse('garbage')).toBe(false)
    expect(f.parse(null)).toBe(false)
    expect(f.serialize(false)).toBeNull()
    expect(f.serialize(true)).toBe('1')
  })

  it('intField normalises negative numbers and non-numeric strings to the default', () => {
    const f = intField(0)
    expect(f.parse('50')).toBe(50)
    expect(f.parse('-5')).toBe(0)
    expect(f.parse('not-a-number')).toBe(0)
    expect(f.parse(null)).toBe(0)
    expect(f.serialize(0)).toBeNull()
    expect(f.serialize(50)).toBe('50')
  })
})

const SCHEMA = {
  period: enumField(['24h', '7d', '30d', 'all'] as const, '7d'),
  q: stringField(''),
  on: boolField(false),
  n: intField(0),
}

describe('parseQuery / serializeQuery', () => {
  it('parses known fields and ignores unknown ones', () => {
    const state = parseQuery('period=30d&q=eth&unknown=x', SCHEMA)
    expect(state).toEqual({ period: '30d', q: 'eth', on: false, n: 0 })
  })

  it('serializes only non-default fields', () => {
    expect(serializeQuery({ period: '7d', q: '', on: false, n: 0 }, SCHEMA)).toBe('')
    expect(serializeQuery({ period: '30d', q: 'eth', on: false, n: 0 }, SCHEMA)).toBe(
      'period=30d&q=eth',
    )
  })

  it('round-trips through parse -> serialize -> parse', () => {
    const original = { period: '24h' as const, q: 'usdc', on: true, n: 5 }
    const search = serializeQuery(original, SCHEMA)
    expect(parseQuery(search, SCHEMA)).toEqual(original)
  })

  it('normalises a malformed query string to defaults instead of throwing', () => {
    expect(() => parseQuery('period=bogus&n=notanumber&on=maybe', SCHEMA)).not.toThrow()
    expect(parseQuery('period=bogus&n=notanumber&on=maybe', SCHEMA)).toEqual({
      period: '7d',
      q: '',
      on: false,
      n: 0,
    })
  })
})

// ---- useHashQueryState: restoration after reload + back/forward (AC1, AC2, AC4) ----

function Harness() {
  const [state, update] = useHashQueryState('history', SCHEMA)
  return React.createElement(
    'div',
    null,
    React.createElement('span', { 'data-testid': 'period' }, state.period),
    React.createElement('span', { 'data-testid': 'q' }, state.q),
    React.createElement('span', { 'data-testid': 'on' }, String(state.on)),
    React.createElement('span', { 'data-testid': 'n' }, String(state.n)),
    React.createElement(
      'button',
      { onClick: () => update({ period: '30d' }) },
      'set-period',
    ),
    React.createElement(
      'button',
      { onClick: () => update({ q: 'eth', n: 5 }) },
      'set-q-and-n',
    ),
  )
}

describe('useHashQueryState', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    window.history.replaceState(null, '', '/')
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(async () => {
    await act(async () => {
      root.unmount()
    })
    document.body.removeChild(container)
  })

  function text(testId: string): string {
    return container.querySelector(`[data-testid="${testId}"]`)!.textContent ?? ''
  }

  it('starts from defaults when the hash has no query', () => {
    window.history.replaceState(null, '', '#/history')
    act(() => root.render(React.createElement(Harness)))
    expect(text('period')).toBe('7d')
    expect(text('q')).toBe('')
    expect(text('on')).toBe('false')
    expect(text('n')).toBe('0')
  })

  it('restores state from an existing hash query, as a reload would (AC1)', () => {
    window.history.replaceState(null, '', '#/history?period=30d&q=eth&on=1&n=5')
    act(() => root.render(React.createElement(Harness)))
    expect(text('period')).toBe('30d')
    expect(text('q')).toBe('eth')
    expect(text('on')).toBe('true')
    expect(text('n')).toBe('5')
  })

  it('normalises an unknown/broken value in the URL to the default without crashing (AC3)', () => {
    window.history.replaceState(null, '', '#/history?period=not-a-real-period&n=abc')
    act(() => root.render(React.createElement(Harness)))
    expect(text('period')).toBe('7d')
    expect(text('n')).toBe('0')
  })

  it('writes updates into the hash, omitting fields at their default value', () => {
    window.history.replaceState(null, '', '#/history')
    act(() => root.render(React.createElement(Harness)))

    const setQAndN = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent === 'set-q-and-n',
    )!
    act(() => setQAndN.click())

    expect(window.location.hash).toBe('#/history?q=eth&n=5')
    expect(text('q')).toBe('eth')
    expect(text('n')).toBe('5')
  })

  it('follows hashchange events, as browser back/forward would (AC2)', () => {
    window.history.replaceState(null, '', '#/history')
    act(() => root.render(React.createElement(Harness)))

    act(() => {
      window.history.replaceState(null, '', '#/history?period=30d')
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    expect(text('period')).toBe('30d')

    act(() => {
      window.history.replaceState(null, '', '#/history')
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    expect(text('period')).toBe('7d')
  })
})
