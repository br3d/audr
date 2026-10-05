import { useState } from 'react'
import type { FormEvent } from 'react'
import {
  fetchIntegrations,
  updateRpc,
  updateQuotes,
  validateIntegration,
  ApiError,
} from '../api/client'
import type { IntegrationEntry, ProviderOption } from '../api/client'
import { useQuery, useQueryClient } from '@tanstack/react-query'

function healthBadge(status: IntegrationEntry['health']['status']) {
  switch (status) {
    case 'ok':
      return <span className="badge badge-ok">OK</span>
    case 'error':
      return <span className="badge badge-error">Error</span>
    case 'validating':
      return <span className="badge badge-info">Validating…</span>
    case 'unvalidated':
      return <span className="badge badge-neutral">Not validated</span>
    default:
      return <span className="badge badge-neutral">{status}</span>
  }
}

/** The source this integration is reading from right now, default or not.
 *
 * Both integrations work with no owner configuration at all, so the summary
 * never says "unavailable" — it names the built-in keyless source and marks
 * it as the default (AUD-440).
 */
function SourceSummary({ entry, fallback }: { entry: IntegrationEntry | null; fallback: string }) {
  const source = entry?.effective_source ?? fallback
  return (
    <div className="row mb-16">
      <span className="text-secondary fw-500">In use: {source}</span>
      {entry?.using_default !== false && <span className="badge badge-neutral">Default</span>}
      {entry?.configured && healthBadge(entry.health.status)}
      {entry?.health.error_message && (
        <span className="text-danger">{entry.health.error_message}</span>
      )}
    </div>
  )
}

function RpcForm({
  current,
  onSaved,
  onValidated,
}: {
  current: IntegrationEntry | null
  onSaved: () => void
  onValidated: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [url, setUrl] = useState('')
  const [allowPrivate, setAllowPrivate] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [validating, setValidating] = useState(false)
  const [validateMsg, setValidateMsg] = useState<string | null>(null)

  async function handleSave(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setError(null)
    if (!url.trim()) {
      setError('RPC URL is required.')
      return
    }
    setSubmitting(true)
    try {
      await updateRpc({
        revision: current?.revision ?? '0',
        url: url.trim(),
        allow_private_host: allowPrivate || undefined,
      })
      setEditing(false)
      setUrl('')
      onSaved()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'An unexpected error occurred.')
    } finally {
      setSubmitting(false)
    }
  }

  async function handleValidate() {
    setValidateMsg(null)
    setValidating(true)
    try {
      await validateIntegration('rpc')
      setValidateMsg('Validation job queued.')
      onValidated()
    } catch (err) {
      setValidateMsg(
        err instanceof ApiError ? `Validation failed: ${err.message}` : 'Validation failed.',
      )
    } finally {
      setValidating(false)
    }
  }

  return (
    <section aria-labelledby="rpc-heading" className="card">
      <h2 id="rpc-heading" className="section-heading mb-8">
        Ethereum RPC Endpoint
      </h2>
      <p className="muted-text mb-16">
        Ethereum is the only supported network. Balances are read through a public endpoint
        that needs no account or key; pointing audr at your own node or a hosted service
        (Infura, Alchemy, …) is an optional upgrade with higher rate limits.
      </p>

      <p className="muted-text mb-16">
        <strong className="text-secondary">Disclosure:</strong> The RPC endpoint sees the
        wallet and contract addresses queried on your behalf. It does not receive your
        password, session, or quote credentials.
      </p>

      <SourceSummary entry={current} fallback="public endpoint (no key required)" />

      {!editing && (
        <div className="btn-group">
          <button type="button" className="btn btn-primary" onClick={() => setEditing(true)}>
            {current?.configured ? 'Change RPC endpoint' : 'Use my own RPC endpoint'}
          </button>

          {current?.configured && (
            <button
              type="button"
              className="btn btn-secondary"
              onClick={handleValidate}
              disabled={validating}
              aria-label="Test RPC connection through the server"
            >
              {validating ? 'Testing…' : 'Test connection'}
            </button>
          )}
        </div>
      )}

      {editing && (
        <form onSubmit={handleSave} noValidate className="form-grid">
          <div className="form-group">
            <label htmlFor="rpc-url" className="form-label">
              RPC URL
            </label>
            <input
              id="rpc-url"
              type="url"
              className="input-folio"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://mainnet.example.com/…"
              required
              disabled={submitting}
              autoComplete="off"
            />
          </div>

          <div className="toggle-row">
            <input
              type="checkbox"
              id="allow-private"
              className="checkbox-folio"
              checked={allowPrivate}
              onChange={(e) => setAllowPrivate(e.target.checked)}
              disabled={submitting}
            />
            <label htmlFor="allow-private" className="toggle-label">
              Allow private / LAN host
            </label>
          </div>
          <p className="muted-text" style={{ marginTop: -8 }}>
            Only enable if you run your own node on a private network. Redirects are always
            denied.
          </p>

          {error !== null && (
            <p role="alert" className="alert alert-danger">
              {error}
            </p>
          )}

          <div className="btn-group">
            <button type="submit" className="btn btn-primary" disabled={submitting}>
              {submitting ? 'Saving…' : 'Save RPC endpoint'}
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => {
                setEditing(false)
                setError(null)
              }}
              disabled={submitting}
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {validateMsg !== null && (
        <p role="status" className="alert alert-info mt-12">
          {validateMsg}
        </p>
      )}
    </section>
  )
}

// Shown only until the server's option list arrives, so the picker is never
// an empty select. Kept in sync with _QUOTE_PROVIDER_OPTIONS in
// backend/src/audr/api/integrations.py.
const FALLBACK_QUOTE_OPTIONS: ProviderOption[] = [
  {
    id: 'coinmarketcap',
    label: 'CoinMarketCap (public endpoints)',
    requires_api_key: false,
    note: 'Used by default and needs no account or API key.',
  },
]

function QuotesForm({
  current,
  onSaved,
  onValidated,
}: {
  current: IntegrationEntry | null
  onSaved: () => void
  onValidated: () => void
}) {
  const options = current?.options?.length ? current.options : FALLBACK_QUOTE_OPTIONS
  const activeProvider = current?.provider ?? options[0].id

  const [editing, setEditing] = useState(false)
  const [provider, setProvider] = useState(activeProvider)
  const [apiKey, setApiKey] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [validating, setValidating] = useState(false)
  const [validateMsg, setValidateMsg] = useState<string | null>(null)

  const selected = options.find((o) => o.id === provider) ?? options[0]

  async function handleSave(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setError(null)
    if (selected.requires_api_key && !apiKey.trim()) {
      setError(`${selected.label} requires an API key.`)
      return
    }
    setSubmitting(true)
    try {
      await updateQuotes({
        revision: current?.revision ?? '0',
        provider: selected.id,
        api_key: selected.requires_api_key ? apiKey.trim() : undefined,
      })
      setEditing(false)
      setApiKey('')
      onSaved()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'An unexpected error occurred.')
    } finally {
      setSubmitting(false)
    }
  }

  async function handleValidate() {
    setValidateMsg(null)
    setValidating(true)
    try {
      await validateIntegration('quotes')
      setValidateMsg('Validation job queued.')
      onValidated()
    } catch (err) {
      setValidateMsg(
        err instanceof ApiError ? `Validation failed: ${err.message}` : 'Validation failed.',
      )
    } finally {
      setValidating(false)
    }
  }

  return (
    <section aria-labelledby="quotes-heading" className="card">
      <h2 id="quotes-heading" className="section-heading mb-8">
        Price Quote Provider
      </h2>
      <p className="muted-text mb-16">
        A quote provider supplies the USD prices your portfolio is valued at. One is always
        in use — the default needs no account or API key — so you only need to come here to
        switch providers or add a key for wider token coverage.
      </p>

      <p className="muted-text mb-16">
        <strong className="text-secondary">Disclosure:</strong> The quote provider receives
        token contract addresses and symbols. It does not receive your wallet addresses,
        password, or session credentials.
      </p>

      <SourceSummary entry={current} fallback="public price endpoint (no key required)" />

      {!editing && (
        <div className="btn-group">
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => {
              setProvider(activeProvider)
              setEditing(true)
            }}
          >
            Change quote provider
          </button>

          {current?.configured && (
            <button
              type="button"
              className="btn btn-secondary"
              onClick={handleValidate}
              disabled={validating}
              aria-label="Test quote provider connection through the server"
            >
              {validating ? 'Testing…' : 'Test quote provider'}
            </button>
          )}
        </div>
      )}

      {editing && (
        <form onSubmit={handleSave} noValidate className="form-grid">
          <div className="form-group">
            <label htmlFor="quotes-provider" className="form-label">
              Provider
            </label>
            <select
              id="quotes-provider"
              className="input-folio"
              value={selected.id}
              onChange={(e) => {
                setProvider(e.target.value)
                setError(null)
              }}
              disabled={submitting}
            >
              {options.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.label}
                  {o.requires_api_key ? ' — API key required' : ' — no API key'}
                </option>
              ))}
            </select>
            <p className="form-hint">{selected.note}</p>
          </div>

          {selected.requires_api_key && (
            <div className="form-group">
              <label htmlFor="quotes-api-key" className="form-label">
                API key
              </label>
              <input
                id="quotes-api-key"
                type="password"
                className="input-folio"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                required
                disabled={submitting}
                autoComplete="new-password"
              />
              <p className="form-hint">
                Stored encrypted and never shown again. To stop using it, switch back to the
                keyless default.
              </p>
            </div>
          )}

          {error !== null && (
            <p role="alert" className="alert alert-danger">
              {error}
            </p>
          )}

          <div className="btn-group">
            <button type="submit" className="btn btn-primary" disabled={submitting}>
              {submitting ? 'Saving…' : 'Save quote provider'}
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => {
                setEditing(false)
                setError(null)
              }}
              disabled={submitting}
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {validateMsg !== null && (
        <p role="status" className="alert alert-info mt-12">
          {validateMsg}
        </p>
      )}
    </section>
  )
}

export default function ConnectionsPage() {
  const queryClient = useQueryClient()
  const { data, error, isLoading } = useQuery({
    queryKey: ['integrations'],
    queryFn: fetchIntegrations,
    // While a validation job is in flight, poll so the badge advances from
    // "Validating…" to OK/Error without a manual reload (AUD-313).
    refetchInterval: (query) =>
      query.state.data?.items.some((i) => i.health.status === 'validating') ? 2000 : false,
  })

  const rpc = data?.items.find((i) => i.kind === 'rpc') ?? null
  const quotes = data?.items.find((i) => i.kind === 'quotes') ?? null

  function handleRpcSaved() {
    void queryClient.invalidateQueries({ queryKey: ['integrations'] })
  }

  function handleQuotesSaved() {
    void queryClient.invalidateQueries({ queryKey: ['integrations'] })
  }

  if (isLoading) {
    return <p aria-busy="true">Loading connection settings…</p>
  }

  if (error) {
    return (
      <p role="alert" className="alert alert-danger">
        Failed to load connection settings.{' '}
        {error instanceof ApiError ? error.message : 'Please try again.'}
      </p>
    )
  }

  return (
    <div style={{ display: 'grid', gap: 20 }}>
      <p className="page-subheading">
        audr works with no configuration here: it reads Ethereum through a public endpoint
        and prices your holdings through a keyless provider. Change either one below. No
        wallet connection, private key, or seed phrase is ever requested.
      </p>
      <RpcForm current={rpc} onSaved={handleRpcSaved} onValidated={handleRpcSaved} />
      <QuotesForm current={quotes} onSaved={handleQuotesSaved} onValidated={handleQuotesSaved} />
    </div>
  )
}
