import { describe, it, expect } from 'vitest'
import type { AlertItem, AllocationItem, Allowance } from '../api/client'
import {
  MARKET_MOVE_THRESHOLD_PCT,
  allowanceSignals,
  marketMoveSignals,
  orderSignals,
} from '../portfolio/inFocus'

const alloc = (o: Partial<AllocationItem>): AllocationItem => ({
  asset_id: 'eth',
  symbol: 'ETH',
  name: 'Ethereum',
  value_usd: '100',
  percentage: '50',
  quantity: '1',
  price_usd: '100',
  price_change_24h_pct: null,
  wallet_count: 1,
  read_status: 'ok',
  included: true,
  ...o,
})

const alert = (o: Partial<AlertItem>): AlertItem => ({
  id: 'a1',
  asset_id: 'eth',
  asset_news_id: 'n1',
  asset_symbol: 'ETH',
  category: 'hack_exploit',
  classifier: 'x',
  created_at: '2026-11-15T00:00:00Z',
  model: null,
  read_at: null,
  severity: 'critical',
  summary: 's',
  ...o,
})

describe('marketMoveSignals', () => {
  it('fires at exactly the threshold and not below it', () => {
    expect(MARKET_MOVE_THRESHOLD_PCT).toBe(10)
    const s = marketMoveSignals([
      alloc({ asset_id: 'a', symbol: 'A', price_change_24h_pct: '10' }),
      alloc({ asset_id: 'b', symbol: 'B', price_change_24h_pct: '-9.99' }),
    ])
    expect(s.map((x) => x.key)).toEqual(['move:a'])
  })

  it('formats negative moves with a real minus and the portfolio share', () => {
    const [s] = marketMoveSignals([alloc({ price_change_24h_pct: '-12.4', percentage: '42.6' })])
    expect(s).toMatchObject({ text: 'ETH −12.4% in 24h · 43% of portfolio' })
  })

  it('omits the share under 1%, and skips excluded, unpriced and unknown-change assets', () => {
    const s = marketMoveSignals([
      alloc({ asset_id: 'a', symbol: 'A', price_change_24h_pct: '15', percentage: '0.5' }),
      alloc({ asset_id: 'b', included: false, price_change_24h_pct: '50' }),
      alloc({ asset_id: 'c', value_usd: null, price_change_24h_pct: '50' }),
      alloc({ asset_id: 'd', price_change_24h_pct: null }),
    ])
    expect(s).toHaveLength(1)
    expect(s[0]).toMatchObject({ text: 'A +15.0% in 24h' })
  })
})

describe('allowanceSignals', () => {
  const al = (o: Partial<Allowance>): Allowance => ({
    wallet_id: 'w',
    token_address: '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48',
    spender_address: '0x1234567890abcdef1234567890abcdef0000abcd',
    raw_amount: '1',
    is_unlimited: true,
    observed_at_block: 1,
    tx_hash: '0x',
    indexed_at: '',
    ...o,
  })

  it('dedupes per token+spender and uses the ticker when known', () => {
    const s = allowanceSignals([al({}), al({ wallet_id: 'w2' })], {
      '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48': 'USDC',
    })
    expect(s).toHaveLength(1)
    expect(s[0]).toMatchObject({ text: 'Unlimited allowance: USDC → 0x1234…abcd' })
  })

  it('falls back to the short token address', () => {
    const [s] = allowanceSignals([al({})], {})
    expect(s).toMatchObject({ text: 'Unlimited allowance: 0xA0b8…eB48 → 0x1234…abcd' })
  })
})

describe('orderSignals', () => {
  it('orders critical, warning, moves, allowances and drops read alerts', () => {
    const moves = marketMoveSignals([alloc({ price_change_24h_pct: '20' })])
    const allows = allowanceSignals(
      [
        {
          wallet_id: 'w',
          token_address: '0xaaaaaaaaaaaaaaaa',
          spender_address: '0xbbbbbbbbbbbbbbbb',
          raw_amount: '1',
          is_unlimited: true,
          observed_at_block: 1,
          tx_hash: '',
          indexed_at: '',
        },
      ],
      {},
    )
    const out = orderSignals(
      [
        alert({ id: 'w1', severity: 'warning' }),
        alert({ id: 'r', severity: 'critical', read_at: '2026-11-15T01:00:00Z' }),
        alert({ id: 'c1', severity: 'critical' }),
      ],
      moves,
      allows,
    )
    expect(out.map((s) => s.key)).toEqual(['alert:c1', 'alert:w1', 'move:eth', expect.stringMatching(/^allowance:/)])
  })
})
