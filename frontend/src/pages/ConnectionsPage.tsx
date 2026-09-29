import { useState } from 'react'
import type { FormEvent } from 'react'
import {
  fetchIntegrations,
  updateRpc,
  updateQuotes,
  validateIntegration,
  ApiError,
} from '../api/client'
import type { IntegrationEntry } from '../api/client'
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

function RpcForm({
  current,
  onSaved,
  onValidated,
}: {
  current: IntegrationEntry | null
  onSaved: () => void
  onValidated: () => void
}) {
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
        Ethereum is the only supported network. Enter an HTTP(S) JSON-RPC URL for your own
        node or a hosted service.
      </p>

      <p className="muted-text mb-16">
        <strong className="text-secondary">Disclosure:</strong> The RPC endpoint sees the
        wallet and contract addresses queried on your behalf. It does not receive your
        password, session, or quote credentials.
      </p>

      {current?.configured && (
        <div className="row mb-16">
          <span className="text-secondary fw-500">
            Current: {current.host_label ?? 'configured (URL masked)'}
          </span>
          {healthBadge(current.health.status)}
          {current.health.error_message && (
            <span className="text-danger">{current.health.error_message}</span>
          )}
        </div>
      )}

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
            {submitting
              ? 'Saving…'
              : current?.configured
                ? 'Replace RPC endpoint'
                : 'Save RPC endpoint'}
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
      </form>

      {validateMsg !== null && (
        <p role="status" className="alert alert-info mt-12">
          {validateMsg}
        </p>
      )}
    </section>
  )
}

function QuotesForm({
  current,
  onSaved,
  onValidated,
}: {
  current: IntegrationEntry | null
  onSaved: () => void
  onValidated: () => void
}) {
  const [provider, setProvider] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [validating, setValidating] = useState(false)
  const [validateMsg, setValidateMsg] = useState<string | null>(null)

  async function handleSave(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setError(null)
    if (!provider.trim()) {
      setError('Provider is required.')
      return
    }
    setSubmitting(true)
    try {
      await updateQuotes({
        revision: current?.revision ?? '0',
        provider: provider.trim(),
        api_key: apiKey.trim() || undefined,
      })
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
        A quote provider supplies USD prices for tokens in your portfolio. Without a
        configured provider, quantities are tracked but values are unavailable.
      </p>

      <p className="muted-text mb-16">
        <strong className="text-secondary">Disclosure:</strong> The quote provider receives
        token contract addresses and symbols. It does not receive your wallet addresses,
        password, or session credentials.
      </p>

      {current?.configured && (
        <div className="row mb-16">
          <span className="text-secondary fw-500">
            Current: {current.provider ?? current.host_label ?? 'configured'}
          </span>
          {healthBadge(current.health.status)}
          {current.health.error_message && (
            <span className="text-danger">{current.health.error_message}</span>
          )}
        </div>
      )}

      <form onSubmit={handleSave} noValidate className="form-grid">
        <div className="form-group">
          <label htmlFor="quotes-provider" className="form-label">
            Provider
          </label>
          <input
            id="quotes-provider"
            type="text"
            className="input-folio"
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
            placeholder="coingecko"
            required
            disabled={submitting}
            autoComplete="off"
          />
        </div>

        <div className="form-group">
          <label htmlFor="quotes-api-key" className="form-label">
            API key <span className="text-muted">(optional)</span>
          </label>
          <input
            id="quotes-api-key"
            type="password"
            className="input-folio"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            disabled={submitting}
            autoComplete="new-password"
          />
          <p className="form-hint">Leave blank to use the public (rate-limited) tier.</p>
        </div>

        {error !== null && (
          <p role="alert" className="alert alert-danger">
            {error}
          </p>
        )}

        <div className="btn-group">
          <button type="submit" className="btn btn-primary" disabled={submitting}>
            {submitting
              ? 'Saving…'
              : current?.configured
                ? 'Replace quote provider'
                : 'Save quote provider'}
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
      </form>

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
        Configure the Ethereum RPC endpoint and price quote provider. No wallet connection,
        private key, or seed phrase is ever requested.
      </p>
      <RpcForm current={rpc} onSaved={handleRpcSaved} onValidated={handleRpcSaved} />
      <QuotesForm current={quotes} onSaved={handleQuotesSaved} onValidated={handleQuotesSaved} />
    </div>
  )
}
