import { useQuery } from '@tanstack/react-query'
import { fetchEvents, fetchAllowances, fetchWallets } from '../api/client'
import type { OnchainEventType, Allowance, WalletItem } from '../api/client'
import { queryKeys } from '../api/queryKeys'
import { errorMessage } from '../api/errorMessage'
import { boolField, enumField, intField, stringField, useHashQueryState } from '../routing'
import EventRow from '../components/EventRow'
import {
  shortenAddress,
  formatQuantity,
  formatIndexedAt,
  etherscanTxUrl,
  etherscanAddressUrl,
} from '../portfolio/events'

const PAGE_SIZE = 50

const EVENT_TYPES: { value: OnchainEventType; label: string }[] = [
  { value: 'transfer_in', label: 'Transfer in' },
  { value: 'transfer_out', label: 'Transfer out' },
]

const EVENTS_SCHEMA = {
  wallet: stringField(''),
  type: enumField<OnchainEventType | ''>(['', 'transfer_in', 'transfer_out'], ''),
  unlimited: boolField(false),
  eventsOffset: intField(0),
  allowancesOffset: intField(0),
}

const shorten = shortenAddress

// raw_amount is an unscaled uint256 decimal string — allowances carry no
// asset enrichment, so this is formatted (thousands separators only, via
// decimal.js for exactness) and explicitly labeled "raw", never coerced
// through Number().
function formatRawAmount(raw: string): string {
  try {
    return formatQuantity(raw)
  } catch {
    return raw
  }
}

function walletLabelMap(wallets: WalletItem[]): Map<string, string> {
  const m = new Map<string, string>()
  for (const w of wallets) {
    m.set(w.id, w.label ?? shorten(w.address))
  }
  return m
}

function Pager({
  offset,
  limit,
  total,
  onPrev,
  onNext,
}: {
  offset: number
  limit: number
  total: number
  onPrev: () => void
  onNext: () => void
}) {
  if (total <= limit && offset === 0) return null
  const from = total === 0 ? 0 : offset + 1
  const to = Math.min(offset + limit, total)
  return (
    <div className="row mt-12" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
      <span className="text-muted">
        {from}–{to} of {total}
      </span>
      <div className="row gap-8">
        <button
          type="button"
          className="btn btn-sm btn-secondary"
          onClick={onPrev}
          disabled={offset === 0}
        >
          Previous
        </button>
        <button
          type="button"
          className="btn btn-sm btn-secondary"
          onClick={onNext}
          disabled={offset + limit >= total}
        >
          Next
        </button>
      </div>
    </div>
  )
}

function AllowanceRow({ allowance, walletLabel }: { allowance: Allowance; walletLabel: string }) {
  return (
    <tr className={allowance.is_unlimited ? 'row-risk' : undefined}>
      <td className="text-secondary">{walletLabel}</td>
      <td className="td-mono">
        <span title={allowance.token_address}>{shorten(allowance.token_address)}</span>
      </td>
      <td className="td-mono">
        {allowance.spender_address ? (
          <a
            href={etherscanAddressUrl(allowance.spender_address)}
            target="_blank"
            rel="noopener noreferrer"
            title={allowance.spender_address}
          >
            {shorten(allowance.spender_address)}
          </a>
        ) : (
          <span className="text-muted">self</span>
        )}
      </td>
      <td>
        <span className="td-mono">
          {formatRawAmount(allowance.raw_amount)} <span className="text-muted">raw</span>
        </span>
        {allowance.is_unlimited && (
          <span
            className="badge badge-danger"
            role="note"
            aria-label="Unlimited allowance — security risk"
            style={{ marginLeft: 8 }}
          >
            Unlimited
          </span>
        )}
      </td>
      <td className="td-mono">
        <a
          href={etherscanTxUrl(allowance.tx_hash)}
          target="_blank"
          rel="noopener noreferrer"
          title={allowance.tx_hash}
        >
          {shorten(allowance.tx_hash)}
        </a>
      </td>
      <td className="text-secondary td-mono">{allowance.observed_at_block}</td>
      <td className="text-secondary">{formatIndexedAt(allowance.indexed_at)}</td>
    </tr>
  )
}

export default function EventsPage() {
  const [
    { wallet: walletFilter, type: eventTypeFilter, unlimited: unlimitedOnly, eventsOffset, allowancesOffset },
    updateQuery,
  ] = useHashQueryState('events', EVENTS_SCHEMA)

  const walletsQuery = useQuery({
    queryKey: queryKeys.walletsForEventsFilter(),
    queryFn: () => fetchWallets(),
  })
  const wallets = walletsQuery.data?.items ?? []
  const labelFor = walletLabelMap(wallets)

  const eventsQuery = useQuery({
    queryKey: queryKeys.events({ walletId: walletFilter, eventType: eventTypeFilter, offset: eventsOffset }),
    queryFn: () =>
      fetchEvents({
        walletId: walletFilter || undefined,
        eventType: eventTypeFilter || undefined,
        limit: PAGE_SIZE,
        offset: eventsOffset,
      }),
  })

  const allowancesQuery = useQuery({
    queryKey: queryKeys.allowances({ walletId: walletFilter, unlimitedOnly, offset: allowancesOffset }),
    queryFn: () =>
      fetchAllowances({
        walletId: walletFilter || undefined,
        unlimitedOnly,
        limit: PAGE_SIZE,
        offset: allowancesOffset,
      }),
  })

  function handleWalletFilterChange(value: string) {
    updateQuery({ wallet: value, eventsOffset: 0, allowancesOffset: 0 })
  }

  function handleEventTypeFilterChange(value: OnchainEventType | '') {
    updateQuery({ type: value, eventsOffset: 0 })
  }

  function handleUnlimitedOnlyChange(value: boolean) {
    updateQuery({ unlimited: value, allowancesOffset: 0 })
  }

  const events = eventsQuery.data?.events ?? []
  const allowances = allowancesQuery.data?.allowances ?? []
  const unlimitedCount = allowances.filter((a) => a.is_unlimited).length

  return (
    <div>
      <p className="page-subheading">
        On-chain activity and approval signals indexed from your tracked addresses. Approval
        amounts are shown unscaled ("raw") since token decimals are not part of allowance data.
      </p>

      <div className="toolbar mb-16">
        <div className="form-group" style={{ minWidth: 220 }}>
          <label htmlFor="events-wallet-filter" className="form-label">
            Wallet
          </label>
          <select
            id="events-wallet-filter"
            className="input-folio"
            value={walletFilter}
            onChange={(e) => handleWalletFilterChange(e.target.value)}
          >
            <option value="">All wallets</option>
            {wallets.map((w) => (
              <option key={w.id} value={w.id}>
                {w.label ?? w.address}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="card mb-16">
        <div className="card-header">
          <div className="card-title">Activity</div>
        </div>

        <div className="toolbar mb-12">
          <div className="form-group" style={{ minWidth: 180 }}>
            <label htmlFor="events-type-filter" className="form-label">
              Type
            </label>
            <select
              id="events-type-filter"
              className="input-folio"
              value={eventTypeFilter}
              onChange={(e) =>
                handleEventTypeFilterChange(e.target.value as OnchainEventType | '')
              }
            >
              <option value="">All types</option>
              {EVENT_TYPES.map((t) => (
                <option key={t.value} value={t.value}>
                  {t.label}
                </option>
              ))}
            </select>
          </div>
        </div>

        {eventsQuery.isLoading && <p aria-busy="true">Loading events…</p>}

        {eventsQuery.error && (
          <p role="alert" className="alert alert-danger">
            Failed to load events.{' '}
            {errorMessage(eventsQuery.error, 'Please try again.')}
          </p>
        )}

        {eventsQuery.data && events.length === 0 && (
          <div className="empty-state">
            <div className="empty-state-icon">⚡</div>
            <div className="empty-state-text">No events found</div>
            <div className="empty-state-hint">
              Either nothing matches the current filters, or the indexer has not processed
              this wallet yet.
            </div>
          </div>
        )}

        {eventsQuery.data && events.length > 0 && (
          <>
            <ul className="event-list" role="list" aria-label="On-chain events">
              {events.map((e) => (
                <EventRow
                  key={e.id}
                  event={e}
                  walletLabel={e.wallet_label ?? labelFor.get(e.wallet_id) ?? shorten(e.wallet_id)}
                />
              ))}
            </ul>
            <Pager
              offset={eventsOffset}
              limit={eventsQuery.data.limit}
              total={eventsQuery.data.total}
              onPrev={() => updateQuery({ eventsOffset: Math.max(0, eventsOffset - PAGE_SIZE) })}
              onNext={() => updateQuery({ eventsOffset: eventsOffset + PAGE_SIZE })}
            />
          </>
        )}
      </div>

      <div className="card">
        <div className="card-header">
          <div className="card-title">Permissions</div>
          {unlimitedCount > 0 && (
            <span className="badge badge-danger" aria-label={`${unlimitedCount} unlimited approvals on this page`}>
              {unlimitedCount} unlimited
            </span>
          )}
        </div>

        <div className="toolbar mb-12">
          <label className="toggle-row">
            <input
              type="checkbox"
              className="checkbox-folio"
              checked={unlimitedOnly}
              onChange={(e) => handleUnlimitedOnlyChange(e.target.checked)}
            />
            <span className="toggle-label">Only unlimited approvals</span>
          </label>
        </div>

        {allowancesQuery.isLoading && <p aria-busy="true">Loading allowances…</p>}

        {allowancesQuery.error && (
          <p role="alert" className="alert alert-danger">
            Failed to load allowances.{' '}
            {errorMessage(allowancesQuery.error, 'Please try again.')}
          </p>
        )}

        {allowancesQuery.data && allowances.length === 0 && (
          <div className="empty-state">
            <div className="empty-state-icon">🛡️</div>
            <div className="empty-state-text">No allowances found</div>
            <div className="empty-state-hint">
              {unlimitedOnly
                ? 'No unlimited approvals matching the current filters.'
                : 'Either nothing matches the current filters, or the indexer has not processed this wallet yet.'}
            </div>
          </div>
        )}

        {allowancesQuery.data && allowances.length > 0 && (
          <>
            <div className="table-container">
              <table className="table-folio" aria-label="Token allowances">
                <thead>
                  <tr>
                    <th scope="col">Wallet</th>
                    <th scope="col">Token</th>
                    <th scope="col">Spender</th>
                    <th scope="col">Approved</th>
                    <th scope="col">Tx</th>
                    <th scope="col">Block</th>
                    <th scope="col">Indexed</th>
                  </tr>
                </thead>
                <tbody>
                  {allowances.map((a) => (
                    <AllowanceRow
                      key={`${a.wallet_id}-${a.token_address}-${a.spender_address}`}
                      allowance={a}
                      walletLabel={labelFor.get(a.wallet_id) ?? shorten(a.wallet_id)}
                    />
                  ))}
                </tbody>
              </table>
            </div>
            <Pager
              offset={allowancesOffset}
              limit={allowancesQuery.data.limit}
              total={allowancesQuery.data.total}
              onPrev={() => updateQuery({ allowancesOffset: Math.max(0, allowancesOffset - PAGE_SIZE) })}
              onNext={() => updateQuery({ allowancesOffset: allowancesOffset + PAGE_SIZE })}
            />
          </>
        )}
      </div>
    </div>
  )
}
