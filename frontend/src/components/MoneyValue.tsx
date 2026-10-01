import { Decimal } from 'decimal.js'

interface Props {
  value: string | null
  currency?: 'USD'
  unknownLabel?: string
  /** Render the integer part large/bold and the fractional part smaller/dim — for metric cards. */
  emphasizeInteger?: boolean
}

export default function MoneyValue({
  value,
  currency = 'USD',
  unknownLabel = 'unknown',
  emphasizeInteger = false,
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

  if (emphasizeInteger) {
    return (
      <span data-testid="money-value" aria-label={`${currency} amount: ${display}`}>
        <span className="money-integer">
          {isNeg ? '-' : ''}${intFormatted}
        </span>
        <span className="money-fraction">.{fracPart}</span>
      </span>
    )
  }

  return (
    <span data-testid="money-value" aria-label={`${currency} amount: ${display}`}>
      {display}
    </span>
  )
}
