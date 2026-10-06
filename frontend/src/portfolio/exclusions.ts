import { Decimal } from 'decimal.js'
import type { AllocationItem, Holding, PortfolioResponse } from '../api/client'

/**
 * Asset ids the owner has toggled in this session, mapped to the exclusion
 * state they asked for — `true` means "exclude", `false` means "include".
 *
 * An entry lives only for the gap between the click and the server payload
 * that agrees with it. It is what makes the allocation table, the share bars
 * and the headline total redraw on the click itself rather than a round trip
 * later (AUD-448); `pruneSettledOverrides` drops each entry as soon as the
 * server catches up, so a stuck entry cannot outlive its refetch and freeze a
 * row the way the old "Applies next snapshot" flag did.
 */
export type ExcludeOverrides = Readonly<Record<string, boolean>>

/** Resolve an asset's effective inclusion: a local override wins over the server. */
function isIncluded(assetId: string, serverIncluded: boolean, overrides: ExcludeOverrides): boolean {
  const override = overrides[assetId]
  return override === undefined ? serverIncluded : !override
}

/**
 * Drop overrides the server payload already reflects.
 *
 * Returns the same object when nothing settled, so this is safe to call from
 * an effect without looping. An asset missing from the payload entirely keeps
 * its override — the server has not spoken about it yet.
 */
export function pruneSettledOverrides(
  data: PortfolioResponse,
  overrides: ExcludeOverrides,
): ExcludeOverrides {
  const entries = Object.entries(overrides)
  if (entries.length === 0) return overrides

  const serverExcluded = new Map<string, boolean>()
  for (const allocation of data.allocations) {
    serverExcluded.set(allocation.asset_id, !allocation.included)
  }

  const unsettled = entries.filter(([assetId, excluded]) => serverExcluded.get(assetId) !== excluded)
  return unsettled.length === entries.length ? overrides : Object.fromEntries(unsettled)
}

/**
 * Re-cut a portfolio payload against the owner's pending exclusion toggles.
 *
 * The server sends every holding it knows about, excluded ones flagged
 * `included: false` and carrying their value (AUD-448), which is what lets
 * this run locally: both directions of the toggle are a recomputation over
 * data already in hand, with no round trip and nothing to wait for.
 *
 * Everything the UI derives from inclusion is recomputed — the priced
 * subtotal, the total and the allocation percentages — so the table and the
 * Portfolio Value card can never disagree about which assets count.
 *
 * `total_usd`'s *null-ness* is a server judgement about snapshot quality, not
 * about exclusion, so it is preserved rather than recomputed: a portfolio
 * whose total was unknowable stays unknowable after a toggle.
 */
export function applyExclusionOverrides(
  data: PortfolioResponse,
  overrides: ExcludeOverrides,
): PortfolioResponse {
  if (Object.keys(overrides).length === 0) return data

  const holdings: Holding[] = data.holdings.map((holding) => {
    const included = isIncluded(holding.asset_id, holding.included, overrides)
    return included === holding.included ? holding : { ...holding, included }
  })

  let pricedSubtotal = new Decimal(0)
  let hasIncludedHolding = false
  for (const holding of holdings) {
    if (!holding.included) continue
    hasIncludedHolding = true
    if (holding.value_usd !== null) {
      pricedSubtotal = pricedSubtotal.plus(holding.value_usd)
    }
  }

  const allocations: AllocationItem[] = data.allocations.map((item) => {
    const included = isIncluded(item.asset_id, item.included, overrides)
    const percentage =
      included && item.value_usd !== null && pricedSubtotal.greaterThan(0)
        ? new Decimal(item.value_usd).dividedBy(pricedSubtotal).times(100).toFixed(2)
        : '0'
    return included === item.included && percentage === item.percentage
      ? item
      : { ...item, included, percentage }
  })

  if (!hasIncludedHolding) {
    return {
      ...data,
      holdings,
      allocations,
      priced_subtotal_usd: null,
      total_usd: null,
      stale_contribution_usd: null,
    }
  }

  const pricedSubtotalStr = pricedSubtotal.toString()

  // The stale contribution is a per-line figure derived from block numbers the
  // payload does not carry, so it cannot be recomputed here. Clamping it to the
  // new subtotal keeps the note from claiming more stale value than the total
  // it qualifies; the next refetch replaces it with the exact figure.
  const staleContribution =
    data.stale_contribution_usd === null
      ? null
      : Decimal.min(new Decimal(data.stale_contribution_usd), pricedSubtotal).toString()

  return {
    ...data,
    holdings,
    allocations,
    priced_subtotal_usd: pricedSubtotalStr,
    total_usd: data.total_usd === null ? null : pricedSubtotalStr,
    stale_contribution_usd: staleContribution,
  }
}
