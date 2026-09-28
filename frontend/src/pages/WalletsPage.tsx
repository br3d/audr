import { useState } from 'react'
import type { FormEvent } from 'react'
import {
  fetchWallets,
  addWallet,
  patchWallet,
  triggerJob,
  ApiError,
} from '../api/client'
import type { WalletItem } from '../api/client'
import { useQuery, useQueryClient } from '@tanstack/react-query'

function AddWalletForm({ onAdded }: { onAdded: () => void }) {
  const [address, setAddress] = useState('')
  const [label, setLabel] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setError(null)
    const trimmed = address.trim()
    if (!trimmed) {
      setError('Wallet address is required.')
      return
    }
    setSubmitting(true)
    try {
      await addWallet({
        address: trimmed,
        label: label.trim() || undefined,
        chain_id: 1,
      })
      setAddress('')
      setLabel('')
      onAdded()
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message)
      } else {
        setError('An unexpected error occurred.')
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} noValidate aria-label="Add tracked address">
      <div>
        <label htmlFor="wallet-address">Ethereum address</label>
        <input
          id="wallet-address"
          type="text"
          value={address}
          onChange={(e) => setAddress(e.target.value)}
          placeholder="0x…"
          required
          disabled={submitting}
          autoComplete="off"
          aria-describedby="wallet-address-hint"
        />
        <p id="wallet-address-hint">
          Any valid public mainnet address. No proof of control is required or
          verified.
        </p>
      </div>
      <div>
        <label htmlFor="wallet-label">Label (optional)</label>
        <input
          id="wallet-label"
          type="text"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          disabled={submitting}
          autoComplete="off"
        />
      </div>
      {error !== null && <p role="alert">{error}</p>}
      <button type="submit" disabled={submitting}>
        {submitting ? 'Adding…' : 'Add tracked address'}
      </button>
    </form>
  )
}

function WalletRow({
  wallet,
  onChanged,
}: {
  wallet: WalletItem
  onChanged: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [label, setLabel] = useState(wallet.label ?? '')
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  async function handleSaveLabel(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setError(null)
    setSaving(true)
    try {
      await patchWallet(wallet.id, { label: label.trim() || undefined })
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
    <li>
      <div>
        <code>{wallet.address}</code>
        {' — '}
        {editing ? (
          <form onSubmit={handleSaveLabel} aria-label="Edit label">
            <input
              type="text"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              disabled={saving}
              autoComplete="off"
            />
            <button type="submit" disabled={saving}>
              {saving ? 'Saving…' : 'Save'}
            </button>
            <button type="button" onClick={() => setEditing(false)} disabled={saving}>
              Cancel
            </button>
          </form>
        ) : (
          <>
            <span>{wallet.label ?? '(no label)'}</span>
            <button type="button" onClick={() => setEditing(true)} disabled={saving}>
              Edit label
            </button>
          </>
        )}
      </div>
      <div>
        <span>
          {wallet.tracking_active ? 'Tracking active' : 'Tracking stopped'}
        </span>
        <button
          type="button"
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
        {!wallet.tracking_active && (
          <p>
            Stopping tracking removes this address from balance calculations but
            retains its historical records.
          </p>
        )}
      </div>
      <div>
        <small>{coverageText}</small>
      </div>
      {error !== null && <p role="alert">{error}</p>}
    </li>
  )
}

export default function WalletsPage() {
  const queryClient = useQueryClient()

  const { data, error, isLoading } = useQuery({
    queryKey: ['wallets'],
    queryFn: () => fetchWallets(),
  })

  const [jobError, setJobError] = useState<string | null>(null)
  const [jobMsg, setJobMsg] = useState<string | null>(null)

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
    return <p>Loading wallets…</p>
  }

  if (error) {
    return (
      <p role="alert">
        Failed to load wallets.{' '}
        {error instanceof ApiError ? error.message : 'Please try again.'}
      </p>
    )
  }

  const wallets = data?.items ?? []

  return (
    <main>
      <h1>Wallets</h1>
      <p>
        Track any valid public mainnet Ethereum address. Labels describe tracked
        addresses and do not imply that this application verified ownership or
        control.
      </p>

      <AddWalletForm onAdded={invalidate} />

      <div>
        <button type="button" onClick={handleRefresh} aria-label="Refresh balances">
          Refresh balances
        </button>
        <button type="button" onClick={handleDiscover} aria-label="Discover tokens">
          Discover tokens
        </button>
        {jobMsg !== null && <p role="status">{jobMsg}</p>}
        {jobError !== null && <p role="alert">{jobError}</p>}
      </div>

      {wallets.length === 0 ? (
        <p>No tracked addresses. Add a wallet above.</p>
      ) : (
        <ul aria-label="Tracked addresses">
          {wallets.map((w) => (
            <WalletRow key={w.id} wallet={w} onChanged={invalidate} />
          ))}
        </ul>
      )}
    </main>
  )
}
