import { useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation } from '@tanstack/react-query'
import { patchWallet, triggerJob } from '../../api/client'
import type { WalletItem } from '../../api/client'
import { errorMessage } from '../../api/errorMessage'
import { IconRefresh, IconTrash } from '../Icons'
import DeleteWalletModal from './DeleteWalletModal'

export default function WalletCard({
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
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const [jobMsg, setJobMsg] = useState<string | null>(null)

  const saveLabelMutation = useMutation({
    mutationFn: () => patchWallet(wallet.id, { label: label.trim() || null }),
    onSuccess: () => {
      setEditing(false)
      onChanged()
    },
  })

  const toggleTrackingMutation = useMutation({
    mutationFn: () => patchWallet(wallet.id, { tracking_active: !wallet.tracking_active }),
    onSuccess: onChanged,
  })

  const refreshMutation = useMutation({
    mutationFn: () => triggerJob('balances', wallet.id),
    onSuccess: () => setJobMsg('Balance refresh queued.'),
  })

  const discoverMutation = useMutation({
    mutationFn: () => triggerJob('discovery', wallet.id),
    onSuccess: () =>
      setJobMsg(
        'Token discovery queued. Refreshing known balances does not discover new contracts outside catalog or manual coverage.',
      ),
  })

  const allMutations = [saveLabelMutation, toggleTrackingMutation, refreshMutation, discoverMutation]

  // These four actions used to share one `error` state, so starting any one
  // of them cleared whatever the others had left behind — keep that so the
  // card never shows a stale error from an unrelated action.
  function resetOtherErrors(except: (typeof allMutations)[number]) {
    for (const m of allMutations) if (m !== except) m.reset()
  }

  function handleSaveLabel(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    resetOtherErrors(saveLabelMutation)
    saveLabelMutation.mutate()
  }

  function handleToggleTracking() {
    resetOtherErrors(toggleTrackingMutation)
    toggleTrackingMutation.mutate()
  }

  function handleRefreshWallet() {
    resetOtherErrors(refreshMutation)
    setJobMsg(null)
    refreshMutation.mutate()
  }

  function handleDiscoverWallet() {
    resetOtherErrors(discoverMutation)
    setJobMsg(null)
    discoverMutation.mutate()
  }

  const saving = saveLabelMutation.isPending || toggleTrackingMutation.isPending
  const jobPending = refreshMutation.isPending ? 'balances' : discoverMutation.isPending ? 'discovery' : null

  const failed = allMutations.find((m) => m.error)
  const error = failed
    ? errorMessage(
        failed.error,
        failed === saveLabelMutation
          ? 'Failed to save label.'
          : failed === toggleTrackingMutation
            ? 'Failed to update tracking.'
            : failed === refreshMutation
              ? 'Failed to queue refresh.'
              : 'Failed to queue discovery.',
      )
    : null

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
                {saveLabelMutation.isPending ? 'Saving…' : 'Save'}
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
          className="btn btn-sm btn-secondary"
          onClick={handleRefreshWallet}
          disabled={saving || !wallet.tracking_active || jobPending !== null}
          aria-label={`Refresh balances for ${wallet.address}`}
          title={wallet.tracking_active ? undefined : 'Resume tracking to refresh balances for this address.'}
        >
          <IconRefresh width={14} height={14} />
          {jobPending === 'balances' ? 'Refreshing…' : 'Refresh balances'}
        </button>
        <button
          type="button"
          className="btn btn-sm btn-secondary"
          onClick={handleDiscoverWallet}
          disabled={saving || !wallet.tracking_active || jobPending !== null}
          aria-label={`Discover tokens for ${wallet.address}`}
          title={wallet.tracking_active ? undefined : 'Resume tracking to discover tokens for this address.'}
        >
          {jobPending === 'discovery' ? 'Discovering…' : 'Discover tokens'}
        </button>
        <button
          type="button"
          className={`btn btn-sm ${wallet.tracking_active ? 'btn-ghost' : 'btn-secondary'}`}
          onClick={handleToggleTracking}
          disabled={saving}
          aria-label={wallet.tracking_active ? 'Stop tracking this address' : 'Resume tracking this address'}
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

      {jobMsg !== null && (
        <p role="status" className="muted-text mt-8">
          {jobMsg}
        </p>
      )}

      {!wallet.tracking_active && (
        <p className="muted-text mt-8">
          Stopping tracking removes this address from balance calculations but retains its historical records.
        </p>
      )}

      {error !== null && (
        <p role="alert" className="alert alert-danger mt-8">
          {error}
        </p>
      )}

      {confirmingDelete && (
        <DeleteWalletModal wallet={wallet} onDeleted={onDeleted} onClose={() => setConfirmingDelete(false)} />
      )}
    </li>
  )
}
