import { useQuery } from '@tanstack/react-query'
import { fetchPortfolio, ApiError } from '../api/client'
import type { Holding, PortfolioQuality } from '../api/client'
import ScanStatus from '../components/ScanStatus'

function QualityBanner({ quality }: { quality: PortfolioQuality }) {
  const warnings: string[] = []

  if (quality.stale_balances) warnings.push('Some balance reads are stale.')
  if (quality.stale_prices) warnings.push('Some price data is stale.')
  if (quality.incomplete)
    warnings.push('Total is incomplete — not all holdings have usable balances and prices.')
  if (quality.mixed_observation_times)
    warnings.push('Holdings were observed at different times.')
  if (quality.discovery_overdue)
    warnings.push('Token discovery is overdue.')
  if (quality.verification_pending)
    warnings.push('Block verification is pending.')
  if (quality.invalidated)
    warnings.push('Some observations have been invalidated.')

  if (warnings.length === 0) return null

  return (
    <ul role="list" aria-label="Data quality warnings">
      {warnings.map((w) => (
        <li key={w} role="note">
          {w}
        </li>
      ))}
    </ul>
  )
}

function HoldingRow({ holding }: { holding: Holding }) {
  const quantityDisplay =
    holding.quantity !== null
      ? holding.quantity
      : holding.raw_balance !== null
        ? `${holding.raw_balance} (raw — decimals unknown)`
        : 'unknown'

  const valueDisplay =
    holding.value_usd !== null
      ? `$${holding.value_usd}`
      : holding.quantity !== null
        ? 'unpriced'
        : 'unknown'

  const readLabel: string =
    holding.read_status === 'ok'
      ? 'Current'
      : holding.read_status === 'stale'
        ? `Stale — last success: ${holding.last_success_at ?? 'never'}`
        : holding.read_status === 'error'
          ? `Read error — last success: ${holding.last_success_at ?? 'never'}`
          : 'Pending'

  return (
    <tr>
      <td>
        {holding.is_native ? (
          'ETH (native)'
        ) : (
          <code>{holding.contract_address}</code>
        )}
      </td>
      <td>{quantityDisplay}</td>
      <td>{valueDisplay}</td>
      <td>
        <span aria-label={`Read status: ${readLabel}`}>{readLabel}</span>
      </td>
      {!holding.included && (
        <td>
          <span aria-label="Excluded from total">Excluded</span>
        </td>
      )}
    </tr>
  )
}

export default function HoldingsPage() {
  const { data, error, isLoading } = useQuery({
    queryKey: ['portfolio'],
    queryFn: () => fetchPortfolio(),
    refetchInterval: 30_000,
  })

  if (isLoading) {
    return <p>Loading holdings…</p>
  }

  if (error) {
    return (
      <p role="alert">
        Failed to load holdings.{' '}
        {error instanceof ApiError ? error.message : 'Please try again.'}
      </p>
    )
  }

  if (!data) return null

  const holdings = data.holdings
  const hasHoldings = holdings.length > 0

  const totalDisplay =
    data.total_usd !== null
      ? `$${data.total_usd}`
      : data.priced_subtotal_usd !== null
        ? `$${data.priced_subtotal_usd} (subtotal — incomplete)`
        : null

  const staleLabelText =
    data.stale_contribution_usd !== null
      ? `Estimated — includes $${data.stale_contribution_usd} from stale reads`
      : null

  return (
    <main>
      <h1>Holdings</h1>

      <section aria-label="Scan actions">
        <ScanStatus runId={null} kind="balances" label="Refresh balances" />
        <ScanStatus runId={null} kind="discovery" label="Discover tokens" />
        <p>
          Refreshing balances does not discover contracts outside catalog or
          manual coverage.
        </p>
      </section>

      {totalDisplay !== null ? (
        <section aria-label="Portfolio total">
          <p>
            <strong>{totalDisplay}</strong>
            {staleLabelText !== null && (
              <> — <span role="note">{staleLabelText}</span></>
            )}
          </p>
        </section>
      ) : (
        <p role="note">Portfolio total is unavailable — no successful scans yet.</p>
      )}

      {data.quality && <QualityBanner quality={data.quality} />}

      {!hasHoldings ? (
        <p>No holdings found. Add a wallet and run a balance refresh.</p>
      ) : (
        <table aria-label="Holdings">
          <caption>Holdings as of last successful scan</caption>
          <thead>
            <tr>
              <th scope="col">Asset</th>
              <th scope="col">Quantity</th>
              <th scope="col">Value (USD)</th>
              <th scope="col">Status</th>
            </tr>
          </thead>
          <tbody>
            {holdings.map((h) => (
              <HoldingRow
                key={`${h.wallet_id}-${h.asset_id}`}
                holding={h}
              />
            ))}
          </tbody>
        </table>
      )}

      {data.balance_block_time && (
        <p>
          <small>
            Balance block time: {data.balance_block_time}
            {data.balance_observed_at && (
              <> · Read completed: {data.balance_observed_at}</>
            )}
          </small>
        </p>
      )}
    </main>
  )
}
