import { useState } from 'react'
import type { FormEvent } from 'react'
import {
  fetchWallets,
  addWallet,
  patchWallet,
  deleteWallet,
  triggerJob,
  ApiError,
} from '../api/client'
import type { WalletItem } from '../api/client'
import { useInfiniteQuery, useQueryClient } from '@tanstack/react-query'
import { IconPlus, IconX, IconRefresh, IconTrash } from '../components/Icons'

const ETH_ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/

function validateEthAddress(value: string): string | null {
  const trimmed = value.trim()
  if (!trimmed) return 'Wallet address is required.'
  if (!ETH_ADDRESS_RE.test(trimmed))
    return 'Enter a valid Ethereum address (0x followed by 40 hex characters).'
  return null
}

function AddWalletModal({
  onAdded,
  onClose,
}: {
  onAdded: () => void
  onClose: () => void
}) {
  const [address, setAddress] = useState('')
  const [label, setLabel] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setError(null)
    const validationError = validateEthAddress(address)
    if (validationError) {
      setError(validationError)
      return
    }
    setSubmitting(true)
    try {
      await addWallet({
        address: address.trim(),
        label: label.trim() || undefined,
        chain_id: 1,
      })
      onAdded()
      onClose()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'An unexpected error occurred.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="Add tracked address"
    >
      <div className="modal-box">
        <div className="modal-header">
          <div className="modal-title">Add Tracked Address</div>
          <button
            type="button"
            className="modal-close"
            onClick={onClose}
            aria-label="Close"
          >
            <IconX width={16} height={16} />
          </button>
        </div>

        <form onSubmit={handleSubmit} noValidate>
          <div className="form-grid">
            <div className="form-group">
              <label htmlFor="wallet-address" className="form-label">
                Ethereum address
              </label>
              <input
                id="wallet-address"
                type="text"
                className="input-folio input-mono"
                value={address}
                onChange={(e) => setAddress(e.target.value)}
                placeholder="0x…"
                required
                disabled={submitting}
                autoComplete="off"
                aria-describedby="wallet-address-hint"
              />
              <p id="wallet-address-hint" className="form-hint">
                Any valid public Ethereum mainnet address (0x + 40 hex characters). No
                proof of control is required or verified.
              </p>
            </div>
            <div className="form-group">
              <label htmlFor="wallet-label" className="form-label">
                Label <span className="text-muted">(optional)</span>
              </label>
              <input
                id="wallet-label"
                type="text"
                className="input-folio"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                disabled={submitting}
                autoComplete="off"
              />
            </div>
          </div>

          {error !== null && (
            <p role="alert" className="alert alert-danger mt-12">
              {error}
            </p>
          )}

          <div className="modal-footer">
            <button
              type="button"
              className="btn btn-ghost"
              onClick={onClose}
              disabled={submitting}
            >
              Cancel
            </button>
            <button type="submit" className="btn btn-primary" disabled={submitting}>
              {submitting ? 'Adding…' : 'Add tracked address'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

function DeleteWalletModal({
  wallet,
  onDeleted,
  onClose,
}: {
  wallet: WalletItem
  onDeleted: (summary: string) => void
  onClose: () => void
}) {
  const [error, setError] = useState<string | null>(null)
  const [deleting, setDeleting] = useState(false)

  async function handleDelete() {
    setError(null)
    setDeleting(true)
    try {
      const result = await deleteWallet(wallet.id)
      const rows = Object.values(result.deleted).reduce((a, b) => a + b, 0)
      onDeleted(
        `Removed ${wallet.label || wallet.address} and ${rows} related record${
          rows === 1 ? '' : 's'
        }.`,
      )
      onClose()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to delete address.')
      setDeleting(false)
    }
  }

  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="Delete tracked address"
    >
      <div className="modal-box">
        <div className="modal-header">
          <div className="modal-title">Delete Tracked Address</div>
          <button
            type="button"
            className="modal-close"
            onClick={onClose}
            aria-label="Close"
            disabled={deleting}
          >
            <IconX width={16} height={16} />
          </button>
        </div>

        <p>
          <strong>{wallet.label || '(no label)'}</strong>
          <br />
          <span className="wallet-address">{wallet.address}</span>
        </p>
        <p className="muted-text mt-8">
          This permanently removes the address and everything derived from it —
          balance observations, discovered token coverage, indexed on-chain events
          and its share of valuation history. It cannot be undone.
        </p>
        <p className="muted-text mt-8">
          If you only want to pause scanning and keep the history, use{' '}
          <strong>Stop tracking</strong> instead.
        </p>

        {error !== null && (
          <p role="alert" className="alert alert-danger mt-12">
            {error}
          </p>
        )}

        <div className="modal-footer">
          <button
            type="button"
            className="btn btn-ghost"
            onClick={onClose}
            disabled={deleting}
          >
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-danger"
            onClick={() => void handleDelete()}
            disabled={deleting}
          >
            {deleting ? 'Deleting…' : 'Delete permanently'}
          </button>
        </div>
      </div>
    </div>
  )
}

function WalletCard({
  wallet,
  onChanged,
  onDeleted,
}: {
  wallet: WalletItem
  onChanged: () => void
  onDeleted: (summary: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [label, setLabel] = useState(wallet.label ?? '')
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [confirmingDelete, setConfirmingDelete] = useState(false)

  async function handleSaveLabel(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setError(null)
    setSaving(true)
    try {
      await patchWallet(wallet.id, { label: label.trim() || null })
      setEditing(false)
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to save label.')
    } finally {
      setSaving(false)
    }
  }

  async function handleToggleTracking() {
    setError(null)
    setSaving(true)
    try {
      await patchWallet(wallet.id, { tracking_active: !wallet.tracking_active })
      onChanged()
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : 'Failed to update tracking.',
      )
    } finally {
      setSaving(false)
    }
  }

  const coverage = wallet.coverage
  let coverageText = 'Discovery not started'
  if (coverage) {
    if (coverage.status === 'completed' && coverage.completed_at) {
      coverageText = `Discovery complete — ${coverage.catalog_total ?? '?'} catalog entries checked`
    } else if (coverage.status === 'running') {
      coverageText = `Discovering — ${coverage.catalog_attempted ?? 0} / ${coverage.catalog_total ?? '?'}`
    } else if (coverage.status === 'partial') {
      coverageText = `Partial discovery — ${coverage.catalog_attempted ?? 0} / ${coverage.catalog_total ?? '?'}`
    }
  }

  return (
    <li className="wallet-card">
      <div className="wallet-card-header">
        <div style={{ flex: 1 }}>
          {editing ? (
            <form onSubmit={handleSaveLabel} aria-label="Edit label" className="row">
              <input
                type="text"
                className="input-folio"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                disabled={saving}
                autoComplete="off"
                style={{ flex: 1 }}
              />
              <button type="submit" className="btn btn-sm btn-primary" disabled={saving}>
                {saving ? 'Saving…' : 'Save'}
              </button>
              <button
                type="button"
                className="btn btn-sm btn-ghost"
                onClick={() => setEditing(false)}
                disabled={saving}
              >
                Cancel
              </button>
            </form>
          ) : (
            <div className="row">
              <span className="wallet-label">{wallet.label ?? '(no label)'}</span>
              <button
                type="button"
                className="btn btn-sm btn-ghost"
                onClick={() => setEditing(true)}
                disabled={saving}
              >
                Edit
              </button>
            </div>
          )}
          <div className="wallet-address mt-4">{wallet.address}</div>
        </div>

        <div>
          {wallet.tracking_active ? (
            <span className="badge badge-ok">Tracking</span>
          ) : (
            <span className="badge badge-neutral">Stopped</span>
          )}
        </div>
      </div>

      <div className="wallet-meta">
        <span className="muted-text">{coverageText}</span>
        <button
          type="button"
          className={`btn btn-sm ${wallet.tracking_active ? 'btn-ghost' : 'btn-secondary'}`}
          onClick={handleToggleTracking}
          disabled={saving}
          aria-label={
            wallet.tracking_active
              ? 'Stop tracking this address'
              : 'Resume tracking this address'
          }
        >
          {wallet.tracking_active ? 'Stop tracking' : 'Resume tracking'}
        </button>
        <button
          type="button"
          className="btn btn-sm btn-danger"
          onClick={() => setConfirmingDelete(true)}
          disabled={saving}
          aria-label={`Delete ${wallet.address} permanently`}
          title="Permanently delete this address and its records"
        >
          <IconTrash width={14} height={14} />
          Delete
        </button>
      </div>

      {!wallet.tracking_active && (
        <p className="muted-text mt-8">
          Stopping tracking removes this address from balance calculations but retains its
          historical records.
        </p>
      )}

      {error !== null && (
        <p role="alert" className="alert alert-danger mt-8">
          {error}
        </p>
      )}

      {confirmingDelete && (
        <DeleteWalletModal
          wallet={wallet}
          onDeleted={onDeleted}
          onClose={() => setConfirmingDelete(false)}
        />
      )}
    </li>
  )
}

export default function WalletsPage() {
  const queryClient = useQueryClient()
  const [showModal, setShowModal] = useState(false)
  const [jobError, setJobError] = useState<string | null>(null)
  const [jobMsg, setJobMsg] = useState<string | null>(null)

  const {
    data,
    error,
    isLoading,
    hasNextPage,
    fetchNextPage,
    isFetchingNextPage,
  } = useInfiniteQuery({
    queryKey: ['wallets'],
    queryFn: ({ pageParam }) => fetchWallets(pageParam as string | undefined),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  })

  function invalidate() {
    void queryClient.invalidateQueries({ queryKey: ['wallets'] })
  }

  async function handleRefresh() {
    setJobError(null)
    setJobMsg(null)
    try {
      await triggerJob('balances')
      setJobMsg('Balance refresh queued.')
    } catch (err) {
      setJobError(
        err instanceof ApiError ? err.message : 'Failed to queue refresh.',
      )
    }
  }

  async function handleDiscover() {
    setJobError(null)
    setJobMsg(null)
    try {
      await triggerJob('discovery')
      setJobMsg(
        'Token discovery queued. Refreshing known balances does not discover new contracts outside catalog or manual coverage.',
      )
    } catch (err) {
      setJobError(
        err instanceof ApiError ? err.message : 'Failed to queue discovery.',
      )
    }
  }

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
          aria-label="Refresh balances"
        >
          <IconRefresh width={14} height={14} />
          Refresh balances
        </button>
        <button
          type="button"
          className="btn btn-secondary"
          onClick={handleDiscover}
          aria-label="Discover tokens"
        >
          Discover tokens
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
                  setJobError(null)
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
