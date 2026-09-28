import { useQuery } from '@tanstack/react-query'
import { fetchPortfolio, ApiError } from '../api/client'
import type { PortfolioQuality } from '../api/client'
import MoneyValue from '../components/MoneyValue'
import AllocationTable from '../components/AllocationTable'
import AllocationChart from '../components/AllocationChart'

function QualityNotices({ quality }: { quality: PortfolioQuality }) {
  const notices: string[] = []

  if (quality.stale_balances) notices.push('Some balance reads are stale.')
  if (quality.stale_prices) notices.push('Some price data is stale.')
  if (quality.incomplete)
    notices.push('Subtotal shown — not all holdings have usable balances and prices.')
  if (quality.mixed_observation_times)
    notices.push('Holdings were observed at different times.')
  if (quality.discovery_overdue) notices.push('Token discovery is overdue.')
  if (quality.verification_pending) notices.push('Block verification is pending.')
  if (quality.invalidated) notices.push('Some observations have been invalidated.')

  if (notices.length === 0) return null

  return (
    <ul role="list" aria-label="Data quality notices">
      {notices.map((n) => (
        <li key={n} role="note">
          {n}
        </li>
      ))}
    </ul>
  )
}

export default function DashboardPage() {
  const { data, error, isLoading } = useQuery({
    queryKey: ['portfolio'],
    queryFn: () => fetchPortfolio(),
    refetchInterval: 30_000,
  })

  if (isLoading) {
    return <p>Loading dashboard…</p>
  }

  if (error) {
    return (
      <p role="alert">
        Failed to load portfolio.{' '}
        {error instanceof ApiError ? error.message : 'Please try again.'}
      </p>
    )
  }

  if (!data) return null

  const hasHoldings = data.holdings.length > 0
  const allExcluded = hasHoldings && data.holdings.every((h) => !h.included)
  const hasPricedAllocations = data.allocations.length > 0
  const isComplete = data.total_usd !== null
  const totalValue = data.total_usd ?? data.priced_subtotal_usd

  return (
    <main>
      <h1>Dashboard</h1>

      <section aria-label="Portfolio total">
        {totalValue !== null ? (
          <p>
            <strong>
              {isComplete ? 'Portfolio total: ' : 'Portfolio subtotal (incomplete): '}
              <MoneyValue value={totalValue} />
            </strong>
            {data.stale_contribution_usd !== null && (
              <>
                {' '}
                <span role="note">
                  Estimated — includes{' '}
                  <MoneyValue value={data.stale_contribution_usd} /> from stale reads
                </span>
              </>
            )}
          </p>
        ) : (
          <p role="note">Portfolio total unavailable — no priced holdings yet.</p>
        )}
      </section>

      <QualityNotices quality={data.quality} />

      {allExcluded && (
        <p role="note">
          All holdings are excluded from the total. Manage exclusions in Assets.
        </p>
      )}

      {!hasHoldings && (
        <p role="note">No holdings found. Add a wallet and run a balance refresh.</p>
      )}

      {hasPricedAllocations ? (
        <section aria-label="Asset allocation">
          <h2>Allocation</h2>
          <AllocationChart items={data.allocations} />
          <AllocationTable items={data.allocations} />
        </section>
      ) : (
        hasHoldings && (
          <p role="note">
            No priced holdings — allocation unavailable. Configure a quote provider in
            Connections.
          </p>
        )
      )}

      {data.balance_block_time !== null && (
        <p>
          <small>
            Balance block time: {data.balance_block_time}
            {data.balance_observed_at !== null && (
              <> · Read completed: {data.balance_observed_at}</>
            )}
          </small>
        </p>
      )}
    </main>
  )
}
