import { Decimal } from 'decimal.js'

interface Props {
  value: string | null
  currency?: 'USD'
  unknownLabel?: string
}

export default function MoneyValue({
  value,
  currency = 'USD',
  unknownLabel = 'unknown',
}: Props) {
  if (value === null) {
    return (
      <span data-testid="money-unknown" aria-label={`${currency} amount: ${unknownLabel}`}>
        {unknownLabel}
      </span>
    )
  }

  const d = new Decimal(value)
  const fixed = d.toFixed(2)
  const dotIdx = fixed.indexOf('.')
  const intPart = fixed.slice(0, dotIdx)
  const fracPart = fixed.slice(dotIdx + 1)

  const isNeg = intPart.startsWith('-')
  const absInt = isNeg ? intPart.slice(1) : intPart
  const intFormatted = absInt.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  const display = `${isNeg ? '-' : ''}$${intFormatted}.${fracPart}`

  return (
    <span data-testid="money-value" aria-label={`${currency} amount: ${display}`}>
      {display}
    </span>
  )
}
