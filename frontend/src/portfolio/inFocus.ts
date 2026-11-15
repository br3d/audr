import { Decimal } from 'decimal.js'
import type { AlertItem, AllocationItem, Allowance } from '../api/client'
import { shortenAddress } from './events'

/** A 24h move at or beyond this many percent (either direction) is a signal. */
export const MARKET_MOVE_THRESHOLD_PCT = 10
/** Panel shows at most this many items; the rest collapse into "+N more". */
export const IN_FOCUS_MAX_ITEMS = 4
/** Mentioning "N% of portfolio" on a market move needs at least this share. */
const PORTFOLIO_SHARE_MIN_PCT = 1

export const ALERT_CATEGORY_LABELS: Record<string, string> = {
  hack_exploit: 'Hack / exploit',
  depeg: 'Depeg',
  delisting: 'Delisting',
  regulatory: 'Regulatory',
  market_move: 'Market move',
  other: 'Other',
}

export type InFocusSignal =
  | { kind: 'alert'; key: string; severity: 'critical' | 'warning'; alert: AlertItem }
  | { kind: 'move'; key: string; text: string }
  | { kind: 'allowance'; key: string; text: string }

export function categoryLabel(category: string): string {
  return ALERT_CATEGORY_LABELS[category] ?? 'Other'
}

function safeDecimal(raw: string | null): Decimal | null {
  if (raw === null) return null
  try {
    const d = new Decimal(raw)
    return d.isFinite() ? d : null
  } catch {
    return null
  }
}

export function marketMoveSignals(allocations: AllocationItem[]): InFocusSignal[] {
  const found: { abs: Decimal; signal: InFocusSignal }[] = []
  for (const a of allocations) {
    if (!a.included || a.value_usd === null) continue
    const change = safeDecimal(a.price_change_24h_pct)
    if (change === null || change.abs().lt(MARKET_MOVE_THRESHOLD_PCT)) continue
    const share = safeDecimal(a.percentage)
    const sign = change.isNegative() ? '−' : '+'
    let text = `${a.symbol} ${sign}${change.abs().toFixed(1)}% in 24h`
    if (share !== null && share.gte(PORTFOLIO_SHARE_MIN_PCT)) text += ` · ${share.toFixed(0)}% of portfolio`
    found.push({ abs: change.abs(), signal: { kind: 'move', key: `move:${a.asset_id}`, text } })
  }
  // Biggest swing first.
  found.sort((x, y) => y.abs.comparedTo(x.abs))
  return found.map((f) => f.signal)
}

export function allowanceSignals(
  allowances: Allowance[],
  symbolByAddress: Record<string, string>,
): InFocusSignal[] {
  const seen = new Set<string>()
  const out: InFocusSignal[] = []
  for (const al of allowances) {
    if (!al.is_unlimited) continue
    const token = al.token_address.toLowerCase()
    const spender = al.spender_address?.toLowerCase() ?? 'self'
    const key = `allowance:${token}:${spender}`
    if (seen.has(key)) continue
    seen.add(key)
    const label = symbolByAddress[token] ?? shortenAddress(al.token_address)
    const to = al.spender_address ? shortenAddress(al.spender_address) : 'itself'
    out.push({ kind: 'allowance', key, text: `Unlimited allowance: ${label} → ${to}` })
  }
  return out
}

/** Critical alerts, warning alerts, market moves, then allowances. */
export function orderSignals(
  alerts: AlertItem[],
  moves: InFocusSignal[],
  allowances: InFocusSignal[],
): InFocusSignal[] {
  const toSignal = (alert: AlertItem, severity: 'critical' | 'warning'): InFocusSignal => ({
    kind: 'alert',
    key: `alert:${alert.id}`,
    severity,
    alert,
  })
  const unread = alerts.filter((a) => a.read_at === null)
  return [
    ...unread.filter((a) => a.severity === 'critical').map((a) => toSignal(a, 'critical')),
    ...unread.filter((a) => a.severity === 'warning').map((a) => toSignal(a, 'warning')),
    ...moves,
    ...allowances,
  ]
}
