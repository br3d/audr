import { useState } from 'react'
import type { FormEvent } from 'react'
import {
  fetchIntegrations,
  updateRpc,
  updateQuotes,
  updateLlm,
  validateIntegration,
  ApiError,
} from '../api/client'
import type { IntegrationEntry, ProviderOption, UpdateLlmInput } from '../api/client'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { queryKeys } from '../api/queryKeys'
import { errorMessage } from '../api/errorMessage'

// "Validation failed: <server message>" when the server names the reason,
// vs. the bare "Validation failed." when it doesn't — distinct enough from
// the generic errorMessage() fallback shape to keep its own ApiError check.
function validationFailureMessage(err: unknown): string {
  return err instanceof ApiError ? `Validation failed: ${err.message}` : 'Validation failed.'
}

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
      {entry?.health.error_message && <span className="text-danger">{entry.health.error_message}</span>}
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
  const [validationError, setValidationError] = useState<string | null>(null)

  const saveMutation = useMutation({
    mutationFn: () =>
      updateRpc({
        revision: current?.revision ?? '0',
        url: url.trim(),
        allow_private_host: allowPrivate || undefined,
      }),
    onSuccess: () => {
      setEditing(false)
      setUrl('')
      onSaved()
    },
  })

  const validateMutation = useMutation({
    mutationFn: () => validateIntegration('rpc'),
    onSuccess: onValidated,
  })

  function handleSave(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setValidationError(null)
    if (!url.trim()) {
      setValidationError('RPC URL is required.')
      return
    }
    saveMutation.mutate()
  }

  const submitting = saveMutation.isPending
  const error =
    validationError ??
    (saveMutation.error ? errorMessage(saveMutation.error, 'An unexpected error occurred.') : null)
  const validating = validateMutation.isPending
  const validateMsg = validateMutation.isSuccess
    ? 'Validation job queued.'
    : validateMutation.error
      ? validationFailureMessage(validateMutation.error)
      : null

  return (
    <section aria-labelledby="rpc-heading" className="card">
      <h2 id="rpc-heading" className="section-heading mb-8">
        Ethereum RPC Endpoint
      </h2>
      <p className="muted-text mb-16">
        Ethereum is the only supported network. Balances are read through a public endpoint that needs no
        account or key; pointing audr at your own node or a hosted service (Infura, Alchemy, …) is an optional
        upgrade with higher rate limits.
      </p>

      <p className="muted-text mb-16">
        <strong className="text-secondary">Disclosure:</strong> The RPC endpoint sees the wallet and contract
        addresses queried on your behalf. It does not receive your password, session, or quote credentials.
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
              onClick={() => validateMutation.mutate()}
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
            Only enable if you run your own node on a private network. Redirects are always denied.
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
                setValidationError(null)
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
  const [validationError, setValidationError] = useState<string | null>(null)

  const selected = options.find((o) => o.id === provider) ?? options[0]

  const saveMutation = useMutation({
    mutationFn: () =>
      updateQuotes({
        revision: current?.revision ?? '0',
        provider: selected.id,
        api_key: selected.requires_api_key ? apiKey.trim() : undefined,
      }),
    onSuccess: () => {
      setEditing(false)
      setApiKey('')
      onSaved()
    },
  })

  const validateMutation = useMutation({
    mutationFn: () => validateIntegration('quotes'),
    onSuccess: onValidated,
  })

  function handleSave(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setValidationError(null)
    if (selected.requires_api_key && !apiKey.trim()) {
      setValidationError(`${selected.label} requires an API key.`)
      return
    }
    saveMutation.mutate()
  }

  const submitting = saveMutation.isPending
  const error =
    validationError ??
    (saveMutation.error ? errorMessage(saveMutation.error, 'An unexpected error occurred.') : null)
  const validating = validateMutation.isPending
  const validateMsg = validateMutation.isSuccess
    ? 'Validation job queued.'
    : validateMutation.error
      ? validationFailureMessage(validateMutation.error)
      : null

  return (
    <section aria-labelledby="quotes-heading" className="card">
      <h2 id="quotes-heading" className="section-heading mb-8">
        Price Quote Provider
      </h2>
      <p className="muted-text mb-16">
        A quote provider supplies the USD prices your portfolio is valued at. One is always in use — the
        default needs no account or API key — so you only need to come here to switch providers or add a key
        for wider token coverage.
      </p>

      <p className="muted-text mb-16">
        audr supports exactly {options.length === 1 ? 'one price provider' : 'these price providers'}:{' '}
        {options.map((o) => o.label).join(', ')}. The list is fixed — supporting another provider takes a code
        change, not a setting here.
      </p>

      <p className="muted-text mb-16">
        <strong className="text-secondary">Disclosure:</strong> The quote provider receives token contract
        addresses and symbols. It does not receive your wallet addresses, password, or session credentials.
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
              onClick={() => validateMutation.mutate()}
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
                setValidationError(null)
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
                Stored encrypted and never shown again. To stop using it, switch back to the keyless default.
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
                setValidationError(null)
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

/** The preset the stored config corresponds to: exact base_url match, else Anthropic, else Custom. */
function initialPresetId(current: IntegrationEntry | null, options: ProviderOption[]): string {
  if (current?.provider === 'anthropic' || !current?.provider) {
    return options.find((o) => o.provider === 'anthropic')?.id ?? 'anthropic'
  }
  const byUrl = options.find(
    (o) => o.provider === 'openai_compatible' && o.base_url && o.base_url === current.base_url,
  )
  return byUrl?.id ?? 'custom'
}

function LlmForm({
  current,
  onSaved,
  onValidated,
}: {
  current: IntegrationEntry | null
  onSaved: () => void
  onValidated: () => void
}) {
  const options = current?.options ?? []
  const [editing, setEditing] = useState(false)
  const [presetId, setPresetId] = useState('anthropic')
  const [baseUrl, setBaseUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [model, setModel] = useState('')
  const [priceIn, setPriceIn] = useState('')
  const [priceOut, setPriceOut] = useState('')
  const [validationError, setValidationError] = useState<string | null>(null)

  const preset = options.find((o) => o.id === presetId) ?? null
  const isAnthropic = (preset?.provider ?? 'anthropic') === 'anthropic'
  const keyStored = current?.configured === true

  function startEditing() {
    const id = initialPresetId(current, options)
    setPresetId(id)
    setBaseUrl(current?.base_url ?? options.find((o) => o.id === id)?.base_url ?? '')
    setModel(current?.model ?? '')
    setPriceIn(current?.price_input_per_mtok ?? '')
    setPriceOut(current?.price_output_per_mtok ?? '')
    setApiKey('')
    setValidationError(null)
    setEditing(true)
  }

  function pickPreset(id: string) {
    setPresetId(id)
    const next = options.find((o) => o.id === id)
    // A preset pre-fills the URL; Custom keeps whatever is typed so it stays editable.
    if (next && id !== 'custom') setBaseUrl(next.base_url ?? '')
  }

  const saveMutation = useMutation({
    mutationFn: () => {
      const key = apiKey.trim()
      const input: UpdateLlmInput = {
        revision: current?.revision ?? '0',
        provider: isAnthropic ? 'anthropic' : 'openai_compatible',
      }
      if (!isAnthropic) {
        input.base_url = baseUrl.trim()
        if (preset?.private_host) input.allow_private_host = true
      }
      if (model.trim()) input.model = model.trim()
      if (key) input.api_key = key // blank = keep the stored key
      if (priceIn.trim()) input.price_input_per_mtok = priceIn.trim()
      if (priceOut.trim()) input.price_output_per_mtok = priceOut.trim()
      return updateLlm(input)
    },
    onSuccess: () => {
      setEditing(false)
      setApiKey('')
      onSaved()
    },
  })

  const validateMutation = useMutation({
    mutationFn: () => validateIntegration('llm'),
    onSuccess: onValidated,
  })

  function handleSave(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setValidationError(null)
    const keyOptional = preset ? !preset.requires_api_key : false
    const sameTarget =
      (current?.provider ?? 'anthropic') === (isAnthropic ? 'anthropic' : 'openai_compatible') &&
      (isAnthropic || (current?.base_url ?? '') === baseUrl.trim())
    if (!apiKey.trim() && !keyOptional && !(keyStored && sameTarget)) {
      setValidationError(`An API key is required for ${preset?.label ?? 'this provider'}.`)
      return
    }
    if (!isAnthropic) {
      if (!baseUrl.trim()) {
        setValidationError('A base URL is required.')
        return
      }
      if (!model.trim() && !preset?.default_model) {
        setValidationError('A model name is required.')
        return
      }
    }
    saveMutation.mutate()
  }

  const submitting = saveMutation.isPending
  const error =
    validationError ??
    (saveMutation.error ? errorMessage(saveMutation.error, 'An unexpected error occurred.') : null)
  const validating = validateMutation.isPending
  const validateMsg = validateMutation.isSuccess
    ? 'Validation job queued.'
    : validateMutation.error
      ? validationFailureMessage(validateMutation.error)
      : null
  const providerName = preset?.label ?? 'the provider'

  return (
    <section aria-labelledby="llm-heading" className="card">
      <h2 id="llm-heading" className="section-heading mb-8">
        AI Analysis
      </h2>
      <p className="muted-text mb-16">
        Optional. Powers the "Smart news analysis" gate on the Schedules page, which scores each incoming news
        article's significance before it is cached. Saving a provider here does not turn the gate on by itself
        — switch it on under Schedules. Any OpenAI-compatible provider works (OpenAI, OpenRouter, Groq,
        Ollama, …) as well as Anthropic.
      </p>

      <p className="muted-text mb-16">
        <strong className="text-secondary">Disclosure:</strong> When the gate is on, the configured provider
        receives each article's headline and full text for scoring. It does not receive your wallet addresses,
        password, or session credentials, and the article text itself is never stored — only the resulting
        summary and score are.
      </p>

      <SourceSummary entry={current} fallback="not configured — articles are ingested unanalyzed" />

      {!editing && (
        <div className="btn-group">
          <button type="button" className="btn btn-primary" onClick={startEditing}>
            {current?.configured ? 'Change LLM provider' : 'Set up LLM provider'}
          </button>

          {current?.configured && (
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => validateMutation.mutate()}
              disabled={validating}
              aria-label="Test the configured LLM provider through the server"
            >
              {validating ? 'Testing…' : 'Test connection'}
            </button>
          )}
        </div>
      )}

      {editing && (
        <form onSubmit={handleSave} noValidate className="form-grid">
          <div className="form-group">
            <label htmlFor="llm-preset" className="form-label">
              Provider
            </label>
            <select
              id="llm-preset"
              className="input-folio"
              value={presetId}
              onChange={(e) => pickPreset(e.target.value)}
              disabled={submitting}
            >
              {options.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.label}
                </option>
              ))}
            </select>
            {preset && <p className="form-hint">{preset.note}</p>}
          </div>

          {!isAnthropic && (
            <div className="form-group">
              <label htmlFor="llm-base-url" className="form-label">
                Base URL
              </label>
              <input
                id="llm-base-url"
                type="url"
                className="input-folio"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                disabled={submitting}
                placeholder="https://host/v1"
                autoComplete="off"
              />
            </div>
          )}

          <div className="form-group">
            <label htmlFor="llm-api-key" className="form-label">
              API key{preset && !preset.requires_api_key ? ' (optional)' : ''}
            </label>
            <input
              id="llm-api-key"
              type="password"
              className="input-folio"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              disabled={submitting}
              autoComplete="new-password"
              placeholder={keyStored ? 'saved — leave blank to keep it' : ''}
            />
            <p className="form-hint">
              {keyStored
                ? 'A key is saved. Leave blank to keep it (only while the provider and base URL are unchanged). '
                : ''}
              Stored encrypted and never shown again.
            </p>
          </div>

          <div className="form-group">
            <label htmlFor="llm-model" className="form-label">
              Model
            </label>
            <input
              id="llm-model"
              type="text"
              className="input-folio"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              disabled={submitting}
              placeholder={preset?.default_model ?? 'model name'}
              autoComplete="off"
            />
          </div>

          <details className="form-group">
            <summary className="form-label">Price per 1M tokens (optional)</summary>
            <label htmlFor="llm-price-in" className="form-label">
              Input, USD
            </label>
            <input
              id="llm-price-in"
              type="text"
              inputMode="decimal"
              className="input-folio"
              value={priceIn}
              onChange={(e) => setPriceIn(e.target.value)}
              disabled={submitting}
            />
            <label htmlFor="llm-price-out" className="form-label">
              Output, USD
            </label>
            <input
              id="llm-price-out"
              type="text"
              inputMode="decimal"
              className="input-folio"
              value={priceOut}
              onChange={(e) => setPriceOut(e.target.value)}
              disabled={submitting}
            />
            <p className="form-hint">Used for the per-run budget. Enter 0 for a local or free model.</p>
          </details>

          {error !== null && (
            <p role="alert" className="alert alert-danger">
              {error}
            </p>
          )}

          <div className="btn-group">
            <button type="submit" className="btn btn-primary" disabled={submitting}>
              {submitting ? 'Saving…' : `Save ${providerName}`}
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => {
                setEditing(false)
                setValidationError(null)
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
    queryKey: queryKeys.integrations(),
    queryFn: fetchIntegrations,
    // While a validation job is in flight, poll so the badge advances from
    // "Validating…" to OK/Error without a manual reload (AUD-313).
    refetchInterval: (query) =>
      query.state.data?.items.some((i) => i.health.status === 'validating') ? 2000 : false,
  })

  const rpc = data?.items.find((i) => i.kind === 'rpc') ?? null
  const quotes = data?.items.find((i) => i.kind === 'quotes') ?? null
  const llm = data?.items.find((i) => i.kind === 'llm') ?? null

  function handleRpcSaved() {
    void queryClient.invalidateQueries({ queryKey: queryKeys.integrations() })
  }

  function handleQuotesSaved() {
    void queryClient.invalidateQueries({ queryKey: queryKeys.integrations() })
  }

  function handleLlmSaved() {
    void queryClient.invalidateQueries({ queryKey: queryKeys.integrations() })
  }

  if (isLoading) {
    return <p aria-busy="true">Loading connection settings…</p>
  }

  if (error) {
    return (
      <p role="alert" className="alert alert-danger">
        Failed to load connection settings. {errorMessage(error, 'Please try again.')}
      </p>
    )
  }

  return (
    <div style={{ display: 'grid', gap: 20 }}>
      <p className="page-subheading">
        audr works with no configuration here: it reads Ethereum through a public endpoint and prices your
        holdings through a keyless provider. Change either one below. No wallet connection, private key, or
        seed phrase is ever requested.
      </p>
      <RpcForm current={rpc} onSaved={handleRpcSaved} onValidated={handleRpcSaved} />
      <QuotesForm current={quotes} onSaved={handleQuotesSaved} onValidated={handleQuotesSaved} />
      <LlmForm current={llm} onSaved={handleLlmSaved} onValidated={handleLlmSaved} />
    </div>
  )
}
