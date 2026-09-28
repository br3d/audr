import { useState, useRef } from 'react'
import type { FormEvent } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  changePassword,
  fetchPurgePreview,
  submitPurge,
  exportPortfolioUrl,
  exportHistoryUrl,
  ApiError,
} from '../api/client'

// --- Password change ---

function PasswordSection() {
  const queryClient = useQueryClient()
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
      // Backend clears the current session cookie on password change.
      // Reset the session query so the app returns to the sign-in screen.
      queryClient.resetQueries({ queryKey: ['session'] })
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : 'Failed to change password.',
      )
    } finally {
      setSaving(false)
    }
  }

  return (
    <section aria-label="Change password">
      <h2>Change password</h2>
      <p>
        Changing the password signs out all active sessions. You will need to sign
        in again after this change.
      </p>
      <form onSubmit={handleSubmit} noValidate aria-label="Change password form">
        <div>
          <label htmlFor="current-password">Current password</label>
          <input
            id="current-password"
            type="password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            disabled={saving}
            required
            autoComplete="current-password"
          />
        </div>
        <div>
          <label htmlFor="new-password">New password</label>
          <input
            id="new-password"
            type="password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
            disabled={saving}
            required
            autoComplete="new-password"
            aria-describedby="new-password-hint"
          />
          <p id="new-password-hint">Minimum 12 characters.</p>
        </div>
        <div>
          <label htmlFor="confirm-password">Confirm new password</label>
          <input
            id="confirm-password"
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            disabled={saving}
            required
            autoComplete="new-password"
          />
        </div>
        {error !== null && <p role="alert">{error}</p>}
        {msg !== null && <p role="status">{msg}</p>}
        <button type="submit" disabled={saving}>
          {saving ? 'Changing password…' : 'Change password'}
        </button>
      </form>
    </section>
  )
}

// --- Export section ---

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
        setExportError(
          err instanceof Error ? err.message : 'Export failed.',
        )
      }
    } finally {
      abortRef.current = null
    }
  }

  function handleAbort() {
    abortRef.current?.abort()
  }

  return (
    <section aria-label="Export data">
      <h2>Export data</h2>
      <p>
        Download a complete export of your portfolio data. Exports include wallet
        addresses, asset metadata, balances, quotes, and valuation history. They do
        not include provider credentials or the owner password.
      </p>

      <form
        onSubmit={(e) => {
          e.preventDefault()
          void handleExport()
        }}
        aria-label="Export options"
      >
        <fieldset>
          <legend>Export content</legend>
          <label>
            <input
              type="radio"
              name="export-kind"
              value="portfolio"
              checked={kind === 'portfolio'}
              onChange={() => setKind('portfolio')}
              disabled={exportState === 'streaming'}
            />
            {' Current portfolio (wallets, assets, holdings, valuation)'}
          </label>
          <label>
            <input
              type="radio"
              name="export-kind"
              value="history"
              checked={kind === 'history'}
              onChange={() => setKind('history')}
              disabled={exportState === 'streaming'}
            />
            {' Full history (all recorded observations and valuations)'}
          </label>
        </fieldset>

        <fieldset>
          <legend>Format</legend>
          <label>
            <input
              type="radio"
              name="export-format"
              value="json"
              checked={format === 'json'}
              onChange={() => setFormat('json')}
              disabled={exportState === 'streaming'}
            />
            {' JSON'}
          </label>
          <label>
            <input
              type="radio"
              name="export-format"
              value="csv"
              checked={format === 'csv'}
              onChange={() => setFormat('csv')}
              disabled={exportState === 'streaming'}
            />
            {' CSV'}
          </label>
        </fieldset>

        {exportState !== 'streaming' ? (
          <button type="submit" aria-label={`Export ${kind} as ${format.toUpperCase()}`}>
            Export {kind} as {format.toUpperCase()}
          </button>
        ) : (
          <button type="button" onClick={handleAbort} aria-label="Cancel export">
            Cancel export
          </button>
        )}
      </form>

      {progress !== null && (
        <p role="status" aria-live="polite">
          {progress}
        </p>
      )}
      {exportError !== null && <p role="alert">{exportError}</p>}
    </section>
  )
}

// --- Purge section ---

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
        `Provider data purge submitted for "${provider}". The affected quote integration has been disabled. Valuations using those quotes will be updated.`,
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
    <section aria-label="Provider data purge">
      <h2>Provider data purge</h2>
      <p>
        Permanently remove all price quote data for a specific provider. This is
        irreversible. On-chain balance records are unaffected. Valuations that used
        the purged quotes will be updated to reflect missing data.
      </p>

      <div>
        <label htmlFor="purge-provider">Provider</label>
        <input
          id="purge-provider"
          type="text"
          value={provider}
          onChange={(e) => {
            setProvider(e.target.value)
            setShowPreview(false)
          }}
          disabled={purging}
          autoComplete="off"
        />
        <button
          type="button"
          onClick={() => setShowPreview(true)}
          disabled={purging || !provider.trim()}
          aria-label="Preview purge impact"
        >
          Preview impact
        </button>
      </div>

      {showPreview && previewQuery.isLoading && (
        <p aria-busy="true">Loading purge preview…</p>
      )}
      {showPreview && previewQuery.error && (
        <p role="alert">
          Failed to load purge preview.{' '}
          {previewQuery.error instanceof ApiError
            ? previewQuery.error.message
            : 'Please try again.'}
        </p>
      )}

      {showPreview && previewQuery.data && (
        <section aria-label="Purge preview" aria-live="polite">
          <h3>Purge preview — what will be deleted</h3>
          <dl>
            <dt>Provider</dt>
            <dd>{previewQuery.data.provider}</dd>
            <dt>Quote observations</dt>
            <dd>{previewQuery.data.quote_observation_count.toLocaleString()}</dd>
            <dt>Quote sets</dt>
            <dd>{previewQuery.data.quote_set_count.toLocaleString()}</dd>
            <dt>Affected valuations</dt>
            <dd>{previewQuery.data.affected_valuation_count.toLocaleString()}</dd>
          </dl>
          <p>
            This action is permanent and cannot be undone. The quote integration will
            be disabled. Re-enabling quotes requires a separate action.
          </p>

          <form onSubmit={handlePurge} aria-label="Confirm purge">
            <div>
              <label htmlFor="purge-password">Current password (required to confirm)</label>
              <input
                id="purge-password"
                type="password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                disabled={purging}
                required
                autoComplete="current-password"
              />
            </div>
            <div>
              <label htmlFor="purge-confirm">
                <input
                  id="purge-confirm"
                  type="checkbox"
                  checked={confirmed}
                  onChange={(e) => setConfirmed(e.target.checked)}
                  disabled={purging}
                />
                {' I understand this is permanent and cannot be undone'}
              </label>
            </div>
            {purgeError !== null && <p role="alert">{purgeError}</p>}
            <button
              type="submit"
              disabled={purging || !confirmed || !confirmPassword}
              aria-label="Confirm and submit purge"
            >
              {purging ? 'Purging…' : 'Confirm and purge provider data'}
            </button>
          </form>
        </section>
      )}

      {purgeMsg !== null && <p role="status">{purgeMsg}</p>}
    </section>
  )
}

// --- Main page ---

export default function AccountDataPage() {
  return (
    <main>
      <h1>Account &amp; Data</h1>
      <p>Manage your password, export your data, and purge provider records.</p>

      <PasswordSection />
      <ExportSection />
      <PurgeSection />
    </main>
  )
}
