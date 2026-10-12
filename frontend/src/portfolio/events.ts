import { Decimal } from 'decimal.js'
import type { OnchainEvent } from '../api/client'

export function shortenAddress(address: string): string {
  if (address.length <= 12) return address
  return `${address.slice(0, 6)}…${address.slice(-4)}`
}

/** Adds thousands separators to an exact decimal string — never routes through Number(). */
export function formatQuantity(decimalString: string): string {
  const fixed = new Decimal(decimalString).toFixed()
  const [intPart, fracPart] = fixed.split('.')
  const withCommas = intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  return fracPart ? `${withCommas}.${fracPart}` : withCommas
}

/**
 * The token quantity for an event's title/subtitle. Prefers the backend's
 * decimal-scaled `amount` (requires a known `symbol`); falls back to the
 * unscaled `raw_amount` when the token is not in our asset catalog — never
 * guesses decimals, since that would silently misrepresent the amount.
 */
export function eventQuantityLabel(event: OnchainEvent): string {
  if (event.amount !== null && event.symbol !== null) {
    return `${formatQuantity(event.amount)} ${event.symbol}`
  }
  return `${formatQuantity(event.raw_amount)} units of ${shortenAddress(event.token_address)}`
}

/**
 * Human-readable sentence for an event row. `counterparty_address === null`
 * means both sides of the transfer are the owner's own wallets (AUD-389
 * item 3c — the owner's address is never stored as a counterparty), so that
 * takes priority over the normal in/out phrasing regardless of whether the
 * token is known.
 */
export function eventTitle(event: OnchainEvent): string {
  if (event.counterparty_address === null) {
    return 'Transfer between your wallets'
  }

  const isIncome = event.event_type === 'transfer_in'
  const verb = isIncome ? 'Received' : 'Sent'

  // Unknown token: the honest fallback names the token contract, not the
  // counterparty — we have no catalog entry to be confident the amount
  // means what we'd imply by also naming who sent/received it.
  if (event.amount === null || event.symbol === null) {
    return `${verb} ${formatQuantity(event.raw_amount)} units of ${shortenAddress(event.token_address)}`
  }

  const preposition = isIncome ? 'from' : 'to'
  const counterparty = shortenAddress(event.counterparty_address)
  return `${verb} ${formatQuantity(event.amount)} ${event.symbol} ${preposition} ${counterparty}`
}

/** `indexed_at` is when our indexer observed the event, not the block time — callers must label it as such. */
export function formatIndexedAt(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function etherscanTxUrl(txHash: string): string {
  return `https://etherscan.io/tx/${txHash}`
}

export function etherscanAddressUrl(address: string): string {
  return `https://etherscan.io/address/${address}`
}
