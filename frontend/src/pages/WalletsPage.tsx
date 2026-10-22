import { useState } from 'react'
import { fetchWallets, triggerJob, ApiError } from '../api/client'
import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { queryKeys } from '../api/queryKeys'
import { errorMessage } from '../api/errorMessage'
import { IconPlus, IconRefresh } from '../components/Icons'
import AddWalletModal from '../components/wallets/AddWalletModal'
import WalletCard from '../components/wallets/WalletCard'

export default function WalletsPage() {
  const queryClient = useQueryClient()
  const [showModal, setShowModal] = useState(false)
  const [jobMsg, setJobMsg] = useState<string | null>(null)

  const {
    data,
    error,
    isLoading,
    hasNextPage,
    fetchNextPage,
    isFetchingNextPage,
  } = useInfiniteQuery({
    queryKey: queryKeys.wallets(),
    queryFn: ({ pageParam }) => fetchWallets(pageParam as string | undefined),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  })

  function invalidate() {
    void queryClient.invalidateQueries({ queryKey: queryKeys.wallets() })
  }

  const refreshAllMutation = useMutation({
    mutationFn: () => triggerJob('balances'),
    onSuccess: () => setJobMsg('Balance refresh queued.'),
  })

  const discoverAllMutation = useMutation({
    mutationFn: () => triggerJob('discovery'),
    onSuccess: () =>
      setJobMsg(
        'Token discovery queued. Refreshing known balances does not discover new contracts outside catalog or manual coverage.',
      ),
  })

  function handleRefresh() {
    discoverAllMutation.reset()
    setJobMsg(null)
    refreshAllMutation.mutate()
  }

  function handleDiscover() {
    refreshAllMutation.reset()
    setJobMsg(null)
    discoverAllMutation.mutate()
  }

  const jobError = refreshAllMutation.error
    ? errorMessage(refreshAllMutation.error, 'Failed to queue refresh.')
    : discoverAllMutation.error
      ? errorMessage(discoverAllMutation.error, 'Failed to queue discovery.')
      : null

  if (isLoading) {
    return <p aria-busy="true">Loading wallets…</p>
  }

  if (error) {
    return (
      <p role="alert" className="alert alert-danger">
        Failed to load wallets.{' '}
        {error instanceof ApiError ? error.message : 'Please try again.'}
      </p>
    )
  }

  const wallets = data?.pages.flatMap((p) => p.items) ?? []

  return (
    <div>
      <p className="page-subheading">
        Track any valid public Ethereum mainnet address. Labels are for your reference and
        do not imply that this application verified ownership or control.
      </p>

      <div className="toolbar mb-16">
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => setShowModal(true)}
        >
          <IconPlus width={14} height={14} />
          Add address
        </button>
        <button
          type="button"
          className="btn btn-secondary"
          onClick={handleRefresh}
          aria-label="Refresh all balances"
          title="Refresh balances for every tracked address — spends an RPC call per address."
        >
          <IconRefresh width={14} height={14} />
          Refresh all balances
        </button>
        <button
          type="button"
          className="btn btn-secondary"
          onClick={handleDiscover}
          aria-label="Discover tokens (all)"
          title="Discover tokens for every tracked address — spends an RPC call per address."
        >
          Discover tokens (all)
        </button>
        {jobMsg !== null && <p role="status" className="muted-text">{jobMsg}</p>}
        {jobError !== null && <p role="alert" className="alert alert-danger">{jobError}</p>}
      </div>

      {wallets.length === 0 ? (
        <div className="empty-state">
          <div className="empty-state-icon">👜</div>
          <div className="empty-state-text">No tracked addresses</div>
          <div className="empty-state-hint">Add an Ethereum address to get started.</div>
        </div>
      ) : (
        <>
          <ul className="wallet-grid" aria-label="Tracked addresses">
            {wallets.map((w) => (
              <WalletCard
                key={w.id}
                wallet={w}
                onChanged={invalidate}
                onDeleted={(summary) => {
                  refreshAllMutation.reset()
                  discoverAllMutation.reset()
                  setJobMsg(summary)
                  invalidate()
                }}
              />
            ))}
          </ul>
          {hasNextPage && (
            <div className="mt-16" style={{ textAlign: 'center' }}>
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => void fetchNextPage()}
                disabled={isFetchingNextPage}
              >
                {isFetchingNextPage ? 'Loading…' : 'Load more'}
              </button>
            </div>
          )}
        </>
      )}

      {showModal && (
        <AddWalletModal
          onAdded={invalidate}
          onClose={() => setShowModal(false)}
        />
      )}
    </div>
  )
}
