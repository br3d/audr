import { describe, it, expect } from 'vitest'
import { Decimal } from 'decimal.js'
import { applyExclusionOverrides, pruneSettledOverrides } from '../portfolio/exclusions'
import type { AllocationItem, Holding, PortfolioResponse } from '../api/client'

function holding(assetId: string, valueUsd: string | null, included = true): Holding {
  return {
    wallet_id: 'w-1',
    asset_id: assetId,
    contract_address: null,
    is_native: false,
    raw_balance: '1',
    decimals: 18,
    quantity: '1',
    price_usd: valueUsd,
    value_usd: valueUsd,
    included,
    metadata_source: 'catalog',
    read_status: 'ok',
    block_time: null,
    observed_at: null,
    last_success_at: null,
  }
}

function allocation(assetId: string, valueUsd: string | null, pct: string, included = true): AllocationItem {
  return {
    asset_id: assetId,
    symbol: assetId.toUpperCase(),
    value_usd: valueUsd,
    percentage: pct,
    quantity: '1',
    price_usd: valueUsd,
    wallet_count: 1,
    read_status: 'ok',
    included,
  }
}

function portfolio(overrides: Partial<PortfolioResponse> = {}): PortfolioResponse {
  return {
    snapshot_id: 's-1',
    membership_revision: null,
    valuation_time: '2026-10-06T10:00:00Z',
    currency: 'USD',
    priced_subtotal_usd: '400',
    total_usd: '400',
    quality: {
      incomplete: false,
      stale_balances: false,
      stale_prices: false,
      mixed_observation_times: false,
      discovery_overdue: false,
      verification_pending: false,
      invalidated: false,
    },
    balance_block: 1,
    balance_block_time: null,
    balance_observed_at: null,
    discovery_completed_at: null,
    holdings: [holding('keep', '100'), holding('drop', '300')],
    allocations: [allocation('keep', '100', '25'), allocation('drop', '300', '75')],
    stale_contribution_usd: null,
    request_id: 'r-1',
    generated_at: '2026-10-06T10:00:00Z',
    ...overrides,
  }
}

describe('applyExclusionOverrides', () => {
  it('returns the payload untouched when nothing is pending', () => {
    const data = portfolio()
    expect(applyExclusionOverrides(data, {})).toBe(data)
  })

  it('drops an excluded asset from the total and re-cuts the percentages', () => {
    const result = applyExclusionOverrides(portfolio(), { drop: true })

    expect(new Decimal(result.total_usd!).toNumber()).toBe(100)
    expect(new Decimal(result.priced_subtotal_usd!).toNumber()).toBe(100)

    const byId = Object.fromEntries(result.allocations.map((a) => [a.asset_id, a]))
    expect(byId.keep.included).toBe(true)
    // The survivor is the whole portfolio now, not a quarter of it.
    expect(new Decimal(byId.keep.percentage).toNumber()).toBe(100)
    expect(byId.drop.included).toBe(false)
    expect(new Decimal(byId.drop.percentage).toNumber()).toBe(0)
    // The excluded row keeps its value, which is what makes Include instant.
    expect(byId.drop.value_usd).toBe('300')
  })

  it('puts a server-excluded asset back without waiting for a refetch', () => {
    const data = portfolio({
      priced_subtotal_usd: '100',
      total_usd: '100',
      holdings: [holding('keep', '100'), holding('drop', '300', false)],
      allocations: [allocation('keep', '100', '100'), allocation('drop', '300', '0', false)],
    })

    const result = applyExclusionOverrides(data, { drop: false })

    expect(new Decimal(result.total_usd!).toNumber()).toBe(400)
    const byId = Object.fromEntries(result.allocations.map((a) => [a.asset_id, a]))
    expect(byId.drop.included).toBe(true)
    expect(new Decimal(byId.drop.percentage).toNumber()).toBe(75)
    expect(new Decimal(byId.keep.percentage).toNumber()).toBe(25)
  })

  it('reports no subtotal at all once everything is excluded', () => {
    const result = applyExclusionOverrides(portfolio(), { keep: true, drop: true })

    expect(result.total_usd).toBeNull()
    expect(result.priced_subtotal_usd).toBeNull()
    expect(result.stale_contribution_usd).toBeNull()
    expect(result.allocations.every((a) => !a.included)).toBe(true)
  })

  it('leaves an unknowable total unknowable', () => {
    const data = portfolio({ total_usd: null })
    const result = applyExclusionOverrides(data, { drop: true })

    // Exclusion does not make an incomplete snapshot computable.
    expect(result.total_usd).toBeNull()
    expect(new Decimal(result.priced_subtotal_usd!).toNumber()).toBe(100)
  })

  it('never claims more stale value than the total it qualifies', () => {
    const data = portfolio({ stale_contribution_usd: '300' })
    const result = applyExclusionOverrides(data, { drop: true })

    expect(new Decimal(result.stale_contribution_usd!).toNumber()).toBe(100)
  })

  it('ignores an unpriced asset when recomputing the subtotal', () => {
    const data = portfolio({
      holdings: [holding('keep', '100'), holding('dust', null)],
      allocations: [allocation('keep', '100', '100'), allocation('dust', null, '0')],
    })

    const result = applyExclusionOverrides(data, { dust: true })
    expect(new Decimal(result.total_usd!).toNumber()).toBe(100)
  })
})

describe('pruneSettledOverrides', () => {
  it('drops an override the server now agrees with', () => {
    const data = portfolio({
      allocations: [allocation('keep', '100', '100'), allocation('drop', '300', '0', false)],
    })
    expect(pruneSettledOverrides(data, { drop: true })).toEqual({})
  })

  it('keeps an override the server has not caught up to', () => {
    const overrides = { drop: true }
    expect(pruneSettledOverrides(portfolio(), overrides)).toBe(overrides)
  })

  it('keeps an override for an asset the payload says nothing about', () => {
    const overrides = { unknown: true }
    expect(pruneSettledOverrides(portfolio(), overrides)).toBe(overrides)
  })

  it('is a no-op when there is nothing pending', () => {
    const overrides = {}
    expect(pruneSettledOverrides(portfolio(), overrides)).toBe(overrides)
  })
})
