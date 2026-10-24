import { describe, it, expect } from 'vitest'
import { eventTitle, eventQuantityLabel, formatQuantity, shortenAddress } from '../portfolio/events'
import type { OnchainEvent } from '../api/client'

function baseEvent(overrides: Partial<OnchainEvent> = {}): OnchainEvent {
  return {
    id: 'ev-1',
    wallet_id: 'w-1',
    tx_hash: '0x1111111111111111111111111111111111111111111111111111111111111111',
    block_number: 100,
    log_index: 0,
    event_type: 'transfer_in',
    token_address: '0x2222222222222222222222222222222222222222',
    counterparty_address: '0x3333333333333333333333333333333333333333',
    raw_amount: '2000000000',
    indexed_at: '2026-06-01T00:00:00Z',
    asset_id: 'asset-1',
    symbol: 'USDC',
    token_name: 'USD Coin',
    decimals: 6,
    amount: '2000',
    price_usd: '1',
    value_usd: '2000',
    wallet_label: null,
    ...overrides,
  }
}

describe('shortenAddress', () => {
  it('keeps short strings intact', () => {
    expect(shortenAddress('0x1234')).toBe('0x1234')
  })

  it('shortens long addresses to head…tail', () => {
    expect(shortenAddress('0x7a1234567890abcdef1234567890abcdefc921')).toBe('0x7a12…c921')
  })
})

describe('formatQuantity', () => {
  it('groups the integer part with thousands separators, preserving fractional digits exactly', () => {
    expect(formatQuantity('2000')).toBe('2,000')
    expect(formatQuantity('1234567.891')).toBe('1,234,567.891')
    expect(formatQuantity('1.5')).toBe('1.5')
  })
})

describe('eventTitle', () => {
  it('describes income with the known symbol and counterparty', () => {
    const title = eventTitle(baseEvent({ event_type: 'transfer_in' }))
    expect(title).toBe('Received 2,000 USDC from 0x3333…3333')
  })

  it('describes an outgoing transfer with the known symbol and counterparty', () => {
    const title = eventTitle(
      baseEvent({
        event_type: 'transfer_out',
        counterparty_address: '0xc4440000000000000000000000000000007d20',
        amount: '1.5',
        symbol: 'ETH',
      }),
    )
    expect(title).toBe('Sent 1.5 ETH to 0xc444…7d20')
  })

  it("names a self-transfer between the owner's own wallets regardless of direction", () => {
    expect(eventTitle(baseEvent({ event_type: 'transfer_in', counterparty_address: null }))).toBe(
      'Transfer between your wallets',
    )
    expect(eventTitle(baseEvent({ event_type: 'transfer_out', counterparty_address: null }))).toBe(
      'Transfer between your wallets',
    )
  })

  it('falls back to raw units of the token contract when the token is not in our catalog — never invents decimals', () => {
    const title = eventTitle(
      baseEvent({
        event_type: 'transfer_in',
        asset_id: null,
        symbol: null,
        token_name: null,
        decimals: null,
        amount: null,
        raw_amount: '12000000000000000000',
        token_address: '0x7a12000000000000000000000000000000c921',
      }),
    )
    expect(title).toBe('Received 12,000,000,000,000,000,000 units of 0x7a12…c921')
    expect(title).not.toContain('NaN')
  })

  it('does not depend on price_usd / value_usd — a missing price never changes the title', () => {
    const withPrice = eventTitle(baseEvent({ value_usd: '2000', price_usd: '1' }))
    const withoutPrice = eventTitle(baseEvent({ value_usd: null, price_usd: null }))
    expect(withoutPrice).toBe(withPrice)
  })
})

describe('eventQuantityLabel', () => {
  it('renders the decimal-scaled amount with symbol when known', () => {
    expect(eventQuantityLabel(baseEvent())).toBe('2,000 USDC')
  })

  it('falls back to raw units of the token address when unknown', () => {
    expect(
      eventQuantityLabel(
        baseEvent({
          symbol: null,
          amount: null,
          raw_amount: '500000000000000000',
          token_address: '0x7a12000000000000000000000000000000c921',
        }),
      ),
    ).toBe('500,000,000,000,000,000 units of 0x7a12…c921')
  })
})
