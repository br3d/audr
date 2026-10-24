import { useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { fetchPurgePreview, submitPurge } from '../../api/client'
import { queryKeys } from '../../api/queryKeys'
import { errorMessage } from '../../api/errorMessage'

export default function PurgeSection() {
  const [provider, setProvider] = useState('coingecko-demo')
  const [showPreview, setShowPreview] = useState(false)
  const [confirmPassword, setConfirmPassword] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [purgeMsg, setPurgeMsg] = useState<string | null>(null)

  const previewQuery = useQuery({
    queryKey: queryKeys.purgePreview(provider),
    queryFn: () => fetchPurgePreview(provider),
    enabled: showPreview,
  })

  const purgeMutation = useMutation({
    mutationFn: () => submitPurge({ provider, confirm: true, current_password: confirmPassword }),
    onSuccess: () => {
      setPurgeMsg(
        `Provider data purge submitted for "${provider}". The affected quote integration has been disabled.`,
      )
      setShowPreview(false)
      setConfirmed(false)
      setConfirmPassword('')
    },
  })

  function handlePurge(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    if (!confirmed || !confirmPassword) return
    purgeMutation.mutate()
  }

  const purging = purgeMutation.isPending
  const purgeError = purgeMutation.error ? errorMessage(purgeMutation.error, 'Purge failed.') : null

  return (
    <section aria-label="Provider data purge" className="card">
      <h2 className="section-heading mb-8">Provider Data Purge</h2>
      <p className="muted-text mb-16">
        Permanently remove all price quote data for a specific provider. This is irreversible. On-chain
        balance records are unaffected.
      </p>

      <div className="row mb-12">
        <div className="form-group" style={{ flex: 1 }}>
          <label htmlFor="purge-provider" className="form-label">
            Provider
          </label>
          <input
            id="purge-provider"
            type="text"
            className="input-folio"
            value={provider}
            onChange={(e) => {
              setProvider(e.target.value)
              setShowPreview(false)
            }}
            disabled={purging}
            autoComplete="off"
          />
        </div>
        <div style={{ alignSelf: 'flex-end' }}>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => setShowPreview(true)}
            disabled={purging || !provider.trim()}
            aria-label="Preview purge impact"
          >
            Preview impact
          </button>
        </div>
      </div>

      {showPreview && previewQuery.isLoading && <p aria-busy="true">Loading purge preview…</p>}
      {showPreview && previewQuery.error && (
        <p role="alert" className="alert alert-danger mb-12">
          Failed to load purge preview. {errorMessage(previewQuery.error, 'Please try again.')}
        </p>
      )}

      {showPreview && previewQuery.data && (
        <section aria-label="Purge preview" aria-live="polite" className="mb-16">
          <div className="section-heading mb-8">What will be deleted</div>
          <div style={{ display: 'grid', gap: 8, marginBottom: 12 }}>
            <div className="row-between">
              <span className="text-secondary">Provider</span>
              <span className="fw-500">{previewQuery.data.provider}</span>
            </div>
            <div className="row-between">
              <span className="text-secondary">Quote observations</span>
              <span className="fw-500">{previewQuery.data.quote_observation_count.toLocaleString()}</span>
            </div>
            <div className="row-between">
              <span className="text-secondary">Quote sets</span>
              <span className="fw-500">{previewQuery.data.quote_set_count.toLocaleString()}</span>
            </div>
            <div className="row-between">
              <span className="text-secondary">Affected valuations</span>
              <span className="fw-500">{previewQuery.data.affected_valuation_count.toLocaleString()}</span>
            </div>
          </div>
          <p className="alert alert-warning mb-12">
            This action is permanent and cannot be undone. The quote integration will be disabled.
          </p>

          <form onSubmit={handlePurge} aria-label="Confirm purge" className="form-grid">
            <div className="form-group">
              <label htmlFor="purge-password" className="form-label">
                Current password (required to confirm)
              </label>
              <input
                id="purge-password"
                type="password"
                className="input-folio"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                disabled={purging}
                required
                autoComplete="current-password"
              />
            </div>
            <label className="toggle-row">
              <input
                id="purge-confirm"
                type="checkbox"
                className="checkbox-folio"
                checked={confirmed}
                onChange={(e) => setConfirmed(e.target.checked)}
                disabled={purging}
              />
              <span className="toggle-label">I understand this is permanent and cannot be undone</span>
            </label>
            {purgeError !== null && (
              <p role="alert" className="alert alert-danger">
                {purgeError}
              </p>
            )}
            <button
              type="submit"
              className="btn btn-danger"
              disabled={purging || !confirmed || !confirmPassword}
              aria-label="Confirm and submit purge"
            >
              {purging ? 'Purging…' : 'Confirm and purge provider data'}
            </button>
          </form>
        </section>
      )}

      {purgeMsg !== null && (
        <p role="status" className="alert alert-success">
          {purgeMsg}
        </p>
      )}
    </section>
  )
}
