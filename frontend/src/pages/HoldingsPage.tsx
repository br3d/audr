import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchPortfolio, ApiError } from '../api/client'
import type { Holding, PortfolioQuality } from '../api/client'
import ScanStatus from '../components/ScanStatus'
import MoneyValue from '../components/MoneyValue'
import { IconSearch } from '../components/Icons'

function QualityBanner({ quality }: { quality: PortfolioQuality }) {
  const warnings: string[] = []

  if (quality.stale_balances) warnings.push('Some balance reads are stale.')
  if (quality.stale_prices) warnings.push('Some price data is stale.')
  if (quality.incomplete)
    warnings.push('Total is incomplete — not all holdings have usable balances and prices.')
  if (quality.mixed_observation_times)
    warnings.push('Holdings were observed at different times.')
  if (quality.discovery_overdue) warnings.push('Token discovery is overdue.')
  if (quality.verification_pending) warnings.push('Block verification is pending.')
  if (quality.invalidated) warnings.push('Some observations have been invalidated.')

  if (warnings.length === 0) return null

  return (
    <ul role="list" aria-label="Data quality warnings" className="notice-list">
      {warnings.map((w) => (
        <li key={w} role="note" className="notice-item">
          {w}
        </li>
      ))}
    </ul>
  )
}

function readBadge(status: Holding['read_status'], lastSuccess: string | null) {
  switch (status) {
    case 'ok':
      return <span className="badge badge-ok">Current</span>
    case 'stale':
      return (
        <span
          className="badge badge-stale"
          title={`Last success: ${lastSuccess ?? 'never'}`}
        >
          Stale
        </span>
      )
    case 'error':
      return (
        <span
          className="badge badge-error"
          title={`Last success: ${lastSuccess ?? 'never'}`}
        >
          Error
        </span>
      )
    case 'pending':
      return <span className="badge badge-neutral">Pending</span>
  }
}

function HoldingRow({ holding }: { holding: Holding }) {
  const quantityDisplay =
    holding.quantity !== null
      ? holding.quantity
      : holding.raw_balance !== null
        ? `${holding.raw_balance} (raw — decimals unknown)`
        : 'unknown'

  return (
    <tr>
      <td>
        {holding.is_native ? (
          <span className="fw-600">ETH</span>
        ) : (
          <span className="td-mono">{holding.contract_address}</span>
        )}
        {!holding.included && (
          <span
            className="badge badge-neutral"
            style={{ marginLeft: 8 }}
            aria-label="Excluded from total"
          >
            excluded
          </span>
        )}
      </td>
      <td className="td-mono">{quantityDisplay}</td>
      <td>
        {holding.value_usd !== null ? (
          <MoneyValue value={holding.value_usd} />
        ) : holding.quantity !== null ? (
          <span className="text-muted">unpriced</span>
        ) : (
          <span className="text-muted">unknown</span>
        )}
      </td>
      <td>
        <span aria-label={`Read status: ${holding.read_status}`}>
          {readBadge(holding.read_status, holding.last_success_at)}
        </span>
      </td>
    </tr>
  )
}

export default function HoldingsPage() {
  const [search, setSearch] = useState('')

  const { data, error, isLoading } = useQuery({
    queryKey: ['portfolio'],
    queryFn: () => fetchPortfolio(),
    refetchInterval: 30_000,
  })

  if (isLoading) {
    return <p aria-busy="true">Loading holdings…</p>
  }

  if (error) {
    return (
      <p role="alert" className="alert alert-danger">
        Failed to load holdings.{' '}
        {error instanceof ApiError ? error.message : 'Please try again.'}
      </p>
    )
  }

  if (!data) return null

  const holdings = data.holdings
  const hasHoldings = holdings.length > 0

  const filtered = search.trim()
    ? holdings.filter((h) => {
        const q = search.toLowerCase()
        if (h.is_native) return 'eth'.includes(q) || 'native'.includes(q)
        return (h.contract_address ?? '').toLowerCase().includes(q)
      })
    : holdings

  const totalDisplay =
    data.total_usd !== null
      ? data.total_usd
      : data.priced_subtotal_usd !== null
        ? data.priced_subtotal_usd
        : null

  const isSubtotal = data.total_usd === null && data.priced_subtotal_usd !== null

  return (
    <div>
      {/* Top bar: total + scan actions */}
      <div className="card mb-20">
        <div className="row-between">
          <div>
            {totalDisplay !== null ? (
              <section aria-label="Portfolio total">
                <div className="metric-label">
                  Portfolio {isSubtotal ? 'Subtotal (incomplete)' : 'Total'}
                </div>
                <div className="metric-value metric-value-sm">
                  <MoneyValue value={totalDisplay} />
                </div>
                {data.stale_contribution_usd !== null && (
                  <div className="metric-note text-warning mt-4">
                    Includes <MoneyValue value={data.stale_contribution_usd} /> from stale reads
                  </div>
                )}
              </section>
            ) : (
              <p role="note" className="text-muted">
                Portfolio total is unavailable — no successful scans yet.
              </p>
            )}
          </div>
          <div className="btn-group">
            <ScanStatus runId={null} kind="balances" label="Refresh balances" />
            <ScanStatus runId={null} kind="discovery" label="Discover tokens" />
          </div>
        </div>
        <p className="muted-text mt-8">
          Refreshing balances does not discover contracts outside catalog or manual coverage.
        </p>
      </div>

      {data.quality && <QualityBanner quality={data.quality} />}

      {!hasHoldings ? (
        <div className="empty-state">
          <div className="empty-state-icon">💎</div>
          <div className="empty-state-text">No holdings found</div>
          <div className="empty-state-hint">Add a wallet and run a balance refresh.</div>
        </div>
      ) : (
        <>
          <div className="toolbar">
            <div className="search-bar">
              <IconSearch className="search-icon" />
              <input
                type="search"
                className="search-input"
                placeholder="Search by contract…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                aria-label="Filter holdings"
              />
            </div>
            <span className="muted-text">
              {filtered.length} of {holdings.length}
            </span>
          </div>

          <div className="table-container">
            <table className="table-folio" aria-label="Holdings">
              <caption className="visually-hidden">Holdings as of last successful scan</caption>
              <thead>
                <tr>
                  <th scope="col">Asset</th>
                  <th scope="col">Quantity</th>
                  <th scope="col">Value (USD)</th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((h) => (
                  <HoldingRow key={`${h.wallet_id}-${h.asset_id}`} holding={h} />
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {data.balance_block_time && (
        <p className="muted-text mt-16">
          Balance block time: {data.balance_block_time}
          {data.balance_observed_at && <> · Read completed: {data.balance_observed_at}</>}
        </p>
      )}
    </div>
  )
}
