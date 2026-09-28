import { useState, useRef } from 'react'
import type { FormEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  changePassword,
  fetchPurgePreview,
  submitPurge,
  exportPortfolioUrl,
  exportHistoryUrl,
  ApiError,
} from '../api/client'

function PasswordSection() {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setError(null)
    setMsg(null)

    if (next.length < 12) {
      setError('New password must be at least 12 characters.')
      return
    }
    if (next !== confirm) {
      setError('New password and confirmation do not match.')
      return
    }

    setSaving(true)
    try {
      await changePassword(current, next)
      setMsg(
        'Password changed. All other sessions have been signed out. Sign in again to continue.',
      )
      setCurrent('')
      setNext('')
      setConfirm('')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to change password.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <section aria-label="Change password" className="card">
      <h2 className="section-heading mb-8">Change Password</h2>
      <p className="muted-text mb-16">
        Changing the password signs out all active sessions. You will need to sign in again.
      </p>
      <form onSubmit={handleSubmit} noValidate aria-label="Change password form" className="form-grid">
        <div className="form-group">
          <label htmlFor="current-password" className="form-label">Current password</label>
          <input
            id="current-password"
            type="password"
            className="input-folio"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            disabled={saving}
            required
            autoComplete="current-password"
          />
        </div>
        <div className="form-group">
          <label htmlFor="new-password" className="form-label">New password</label>
          <input
            id="new-password"
            type="password"
            className="input-folio"
            value={next}
            onChange={(e) => setNext(e.target.value)}
            disabled={saving}
            required
            autoComplete="new-password"
            aria-describedby="new-password-hint"
          />
          <p id="new-password-hint" className="form-hint">Minimum 12 characters.</p>
        </div>
        <div className="form-group">
          <label htmlFor="confirm-password" className="form-label">Confirm new password</label>
          <input
            id="confirm-password"
            type="password"
            className="input-folio"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            disabled={saving}
            required
            autoComplete="new-password"
          />
        </div>
        {error !== null && <p role="alert" className="alert alert-danger">{error}</p>}
        {msg !== null && <p role="status" className="alert alert-success">{msg}</p>}
        <button type="submit" className="btn btn-primary" disabled={saving}>
          {saving ? 'Changing password…' : 'Change password'}
        </button>
      </form>
    </section>
  )
}

type ExportKind = 'portfolio' | 'history'
type ExportFormat = 'json' | 'csv'
type ExportState = 'idle' | 'streaming' | 'done' | 'error'

function ExportSection() {
  const [kind, setKind] = useState<ExportKind>('portfolio')
  const [format, setFormat] = useState<ExportFormat>('json')
  const [exportState, setExportState] = useState<ExportState>('idle')
  const [progress, setProgress] = useState<string | null>(null)
  const [exportError, setExportError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  function buildUrl(): string {
    if (kind === 'portfolio') return exportPortfolioUrl(format)
    return exportHistoryUrl(format)
  }

  async function handleExport() {
    setExportState('streaming')
    setProgress(null)
    setExportError(null)

    const controller = new AbortController()
    abortRef.current = controller

    try {
      const response = await fetch(buildUrl(), {
        credentials: 'same-origin',
        signal: controller.signal,
      })

      if (!response.ok) {
        throw new Error(`Export failed: HTTP ${response.status}`)
      }

      const disposition = response.headers.get('Content-Disposition') ?? ''
      const match = disposition.match(/filename="?([^";\n]+)"?/)
      const filename = match?.[1] ?? `audr-export-${kind}.${format}`

      const reader = response.body?.getReader()
      const chunks: Uint8Array<ArrayBuffer>[] = []
      let bytesReceived = 0

      if (reader) {
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          chunks.push(value)
          bytesReceived += value.length
          setProgress(`Downloading… ${(bytesReceived / 1024).toFixed(1)} KB`)
        }
      }

      const blob = new Blob(chunks, {
        type: format === 'json' ? 'application/json' : 'text/csv',
      })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)

      setExportState('done')
      setProgress(`Download complete — ${(bytesReceived / 1024).toFixed(1)} KB`)
    } catch (err) {
      if ((err as Error).name === 'AbortError') {
        setExportState('idle')
        setProgress(null)
      } else {
        setExportState('error')
        setExportError(err instanceof Error ? err.message : 'Export failed.')
      }
    } finally {
      abortRef.current = null
    }
  }

  function handleAbort() {
    abortRef.current?.abort()
  }

  return (
    <section aria-label="Export data" className="card">
      <h2 className="section-heading mb-8">Export Data</h2>
      <p className="muted-text mb-16">
        Download a complete export of your portfolio data. Exports include wallet addresses,
        asset metadata, balances, quotes, and valuation history. They do not include provider
        credentials or the owner password.
      </p>

      <form
        onSubmit={(e) => {
          e.preventDefault()
          void handleExport()
        }}
        aria-label="Export options"
        className="form-grid"
      >
        <div>
          <div className="form-label mb-8">Export content</div>
          <div style={{ display: 'grid', gap: 8 }}>
            <label className="toggle-row">
              <input
                type="radio"
                name="export-kind"
                value="portfolio"
                checked={kind === 'portfolio'}
                onChange={() => setKind('portfolio')}
                disabled={exportState === 'streaming'}
                className="checkbox-folio"
              />
              <span className="toggle-label">Current portfolio (wallets, assets, holdings, valuation)</span>
            </label>
            <label className="toggle-row">
              <input
                type="radio"
                name="export-kind"
                value="history"
                checked={kind === 'history'}
                onChange={() => setKind('history')}
                disabled={exportState === 'streaming'}
                className="checkbox-folio"
              />
              <span className="toggle-label">Full history (all recorded observations and valuations)</span>
            </label>
          </div>
        </div>

        <div>
          <div className="form-label mb-8">Format</div>
          <div className="row gap-12">
            <label className="toggle-row">
              <input
                type="radio"
                name="export-format"
                value="json"
                checked={format === 'json'}
                onChange={() => setFormat('json')}
                disabled={exportState === 'streaming'}
                className="checkbox-folio"
              />
              <span className="toggle-label">JSON</span>
            </label>
            <label className="toggle-row">
              <input
                type="radio"
                name="export-format"
                value="csv"
                checked={format === 'csv'}
                onChange={() => setFormat('csv')}
                disabled={exportState === 'streaming'}
                className="checkbox-folio"
              />
              <span className="toggle-label">CSV</span>
            </label>
          </div>
        </div>

        {exportState !== 'streaming' ? (
          <button
            type="submit"
            className="btn btn-primary"
            style={{ width: 'fit-content' }}
            aria-label={`Export ${kind} as ${format.toUpperCase()}`}
          >
            Export {kind} as {format.toUpperCase()}
          </button>
        ) : (
          <button
            type="button"
            className="btn btn-ghost"
            onClick={handleAbort}
            aria-label="Cancel export"
          >
            Cancel export
          </button>
        )}
      </form>

      {progress !== null && (
        <p role="status" aria-live="polite" className="muted-text mt-12">
          {progress}
        </p>
      )}
      {exportError !== null && (
        <p role="alert" className="alert alert-danger mt-12">
          {exportError}
        </p>
      )}
    </section>
  )
}

function PurgeSection() {
  const [provider, setProvider] = useState('coingecko-demo')
  const [showPreview, setShowPreview] = useState(false)
  const [confirmPassword, setConfirmPassword] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [purging, setPurging] = useState(false)
  const [purgeMsg, setPurgeMsg] = useState<string | null>(null)
  const [purgeError, setPurgeError] = useState<string | null>(null)

  const previewQuery = useQuery({
    queryKey: ['purge-preview', provider],
    queryFn: () => fetchPurgePreview(provider),
    enabled: showPreview,
  })

  async function handlePurge(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    if (!confirmed || !confirmPassword) return
    setPurgeError(null)
    setPurging(true)
    try {
      await submitPurge({ provider, confirm: true, current_password: confirmPassword })
      setPurgeMsg(
        `Provider data purge submitted for "${provider}". The affected quote integration has been disabled.`,
      )
      setShowPreview(false)
      setConfirmed(false)
      setConfirmPassword('')
    } catch (err) {
      setPurgeError(err instanceof ApiError ? err.message : 'Purge failed.')
    } finally {
      setPurging(false)
    }
  }

  return (
    <section aria-label="Provider data purge" className="card">
      <h2 className="section-heading mb-8">Provider Data Purge</h2>
      <p className="muted-text mb-16">
        Permanently remove all price quote data for a specific provider. This is
        irreversible. On-chain balance records are unaffected.
      </p>

      <div className="row mb-12">
        <div className="form-group" style={{ flex: 1 }}>
          <label htmlFor="purge-provider" className="form-label">Provider</label>
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

      {showPreview && previewQuery.isLoading && (
        <p aria-busy="true">Loading purge preview…</p>
      )}
      {showPreview && previewQuery.error && (
        <p role="alert" className="alert alert-danger mb-12">
          Failed to load purge preview.{' '}
          {previewQuery.error instanceof ApiError
            ? previewQuery.error.message
            : 'Please try again.'}
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
            This action is permanent and cannot be undone. The quote integration will be
            disabled.
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
              <span className="toggle-label">
                I understand this is permanent and cannot be undone
              </span>
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

export default function AccountDataPage() {
  return (
    <div style={{ display: 'grid', gap: 20 }}>
      <p className="page-subheading">
        Manage your password, export your data, and purge provider records.
      </p>
      <PasswordSection />
      <ExportSection />
      <PurgeSection />
    </div>
  )
}
