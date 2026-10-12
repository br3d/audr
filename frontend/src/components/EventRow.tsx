import type { OnchainEvent } from '../api/client'
import MoneyValue from './MoneyValue'
import { IconArrowDown, IconArrowUp } from './Icons'
import { eventTitle, eventQuantityLabel, formatIndexedAt, etherscanTxUrl } from '../portfolio/events'

interface Props {
  event: OnchainEvent
  walletLabel: string
}

export default function EventRow({ event, walletLabel }: Props) {
  const isIncome = event.event_type === 'transfer_in'

  return (
    <li className="event-row">
      <div
        className={`event-icon ${isIncome ? 'event-icon-positive' : 'event-icon-negative'}`}
        aria-hidden="true"
      >
        {isIncome ? <IconArrowDown /> : <IconArrowUp />}
      </div>
      <div className="event-main">
        <div className="event-title-row">
          <a
            className="event-title-link"
            href={etherscanTxUrl(event.tx_hash)}
            target="_blank"
            rel="noopener noreferrer"
            title={event.tx_hash}
          >
            {eventTitle(event)}
          </a>
          {event.value_usd === null ? (
            <span className="event-amount text-muted">—</span>
          ) : (
            <span
              className={`event-amount ${isIncome ? 'text-success' : 'text-danger'}`}
              title="Valued at the latest known price — not the price at the time of this event."
            >
              {isIncome ? '+' : '−'}
              <MoneyValue value={event.value_usd} />
            </span>
          )}
        </div>
        <div className="event-subtitle">
          {walletLabel} · {eventQuantityLabel(event)} · indexed {formatIndexedAt(event.indexed_at)}
        </div>
      </div>
    </li>
  )
}
