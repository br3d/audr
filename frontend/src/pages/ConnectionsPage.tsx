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

function RpcForm({
  current,
  onSaved,
}: {
  current: IntegrationEntry | null
  onSaved: () => void
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
      if (err instanceof ApiError) {
        setError(err.message)
      } else {
        setError('An unexpected error occurred.')
      }
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
    } catch (err) {
      if (err instanceof ApiError) {
        setValidateMsg(`Validation failed: ${err.message}`)
      } else {
        setValidateMsg('Validation failed.')
      }
    } finally {
      setValidating(false)
    }
  }

  return (
    <section aria-labelledby="rpc-heading">
      <h2 id="rpc-heading">Ethereum RPC endpoint</h2>

      <p>
        Ethereum is the only supported network. Enter an HTTP(S) JSON-RPC URL
        for your own node or a hosted service.
      </p>

      <p>
        <strong>Disclosure:</strong> The RPC endpoint sees the wallet addresses
        and contract addresses queried on your behalf. It does not receive your
        password, session, or quote credentials.
      </p>

      {current?.configured && (
        <p>
          Current endpoint:{' '}
          <strong>
            {current.host_label ?? 'configured (URL masked)'}
          </strong>
          {' — '}
          health:{' '}
          <span aria-label={`RPC health: ${current.health.status}`}>
            {current.health.status}
          </span>
          {current.health.error_message && (
            <> — {current.health.error_message}</>
          )}
        </p>
      )}

      <form onSubmit={handleSave} noValidate>
        <div>
          <label htmlFor="rpc-url">
            RPC URL
          </label>
          <input
            id="rpc-url"
            type="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://mainnet.example.com/…"
            required
            disabled={submitting}
            autoComplete="off"
          />
        </div>

        <div>
          <label>
            <input
              type="checkbox"
              checked={allowPrivate}
              onChange={(e) => setAllowPrivate(e.target.checked)}
              disabled={submitting}
            />
            {' Allow private / LAN host'}
          </label>
          <p>
            Only enable if you run your own node on a private network. Redirects
            are always denied.
          </p>
        </div>

        {error !== null && <p role="alert">{error}</p>}

        <button type="submit" disabled={submitting}>
          {submitting ? 'Saving…' : current?.configured ? 'Replace RPC endpoint' : 'Save RPC endpoint'}
        </button>
      </form>

      {current?.configured && (
        <div>
          <button
            type="button"
            onClick={handleValidate}
            disabled={validating}
            aria-label="Test RPC connection through the server"
          >
            {validating ? 'Testing…' : 'Test connection'}
          </button>
          {validateMsg !== null && <p role="status">{validateMsg}</p>}
          <p>
            Connection is tested through the server. Wrong network and
            unsupported safe-block capability receive actionable messages.
          </p>
        </div>
      )}
    </section>
  )
}

function QuotesForm({
  current,
  onSaved,
}: {
  current: IntegrationEntry | null
  onSaved: () => void
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
      if (err instanceof ApiError) {
        setError(err.message)
      } else {
        setError('An unexpected error occurred.')
      }
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
    } catch (err) {
      if (err instanceof ApiError) {
        setValidateMsg(`Validation failed: ${err.message}`)
      } else {
        setValidateMsg('Validation failed.')
      }
    } finally {
      setValidating(false)
    }
  }

  return (
    <section aria-labelledby="quotes-heading">
      <h2 id="quotes-heading">Price quote provider</h2>

      <p>
        A quote provider supplies USD prices for tokens in your portfolio. Without
        a configured provider, quantities are tracked but values are unavailable.
      </p>

      <p>
        <strong>Disclosure:</strong> The quote provider receives the token
        contract addresses and symbols queried on your behalf. It does not
        receive your wallet addresses, password, or session credentials.
        Use a provider whose data-sharing policy you are comfortable with.
      </p>

      {current?.configured && (
        <p>
          Current provider:{' '}
          <strong>{current.provider ?? current.host_label ?? 'configured'}</strong>
          {' — '}
          health:{' '}
          <span aria-label={`Quote provider health: ${current.health.status}`}>
            {current.health.status}
          </span>
          {current.health.error_message && (
            <> — {current.health.error_message}</>
          )}
        </p>
      )}

      <form onSubmit={handleSave} noValidate>
        <div>
          <label htmlFor="quotes-provider">Provider</label>
          <input
            id="quotes-provider"
            type="text"
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
            placeholder="coingecko"
            required
            disabled={submitting}
            autoComplete="off"
          />
        </div>

        <div>
          <label htmlFor="quotes-api-key">API key (optional)</label>
          <input
            id="quotes-api-key"
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            disabled={submitting}
            autoComplete="new-password"
          />
          <p>Leave blank to use the public (rate-limited) tier.</p>
        </div>

        {error !== null && <p role="alert">{error}</p>}

        <button type="submit" disabled={submitting}>
          {submitting
            ? 'Saving…'
            : current?.configured
              ? 'Replace quote provider'
              : 'Save quote provider'}
        </button>
      </form>

      {current?.configured && (
        <div>
          <button
            type="button"
            onClick={handleValidate}
            disabled={validating}
            aria-label="Test quote provider connection through the server"
          >
            {validating ? 'Testing…' : 'Test quote provider'}
          </button>
          {validateMsg !== null && <p role="status">{validateMsg}</p>}
        </div>
      )}
    </section>
  )
}

export default function ConnectionsPage() {
  const queryClient = useQueryClient()
  const { data, error, isLoading } = useQuery({
    queryKey: ['integrations'],
    queryFn: fetchIntegrations,
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
    return <p>Loading connection settings…</p>
  }

  if (error) {
    return (
      <p role="alert">
        Failed to load connection settings.{' '}
        {error instanceof ApiError ? error.message : 'Please try again.'}
      </p>
    )
  }

  return (
    <main>
      <h1>Connections</h1>
      <p>
        Configure the Ethereum RPC endpoint used to read wallet balances and
        the price quote provider used to value holdings. No wallet connection,
        private key, or seed phrase is ever requested.
      </p>
      <RpcForm current={rpc} onSaved={handleRpcSaved} />
      <QuotesForm current={quotes} onSaved={handleQuotesSaved} />
    </main>
  )
}
