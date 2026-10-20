import { useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  fetchApiTokens,
  createApiToken,
  revokeApiToken,
  ApiError,
} from '../api/client'
import type { ApiTokenItem } from '../api/client'

function formatDate(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  })
}

function isExpired(token: ApiTokenItem): boolean {
  return token.expires_at !== null && new Date(token.expires_at).getTime() <= Date.now()
}

function TokenRow({
  token,
  onRevoke,
}: {
  token: ApiTokenItem
  onRevoke: (id: string) => void
}) {
  const [confirming, setConfirming] = useState(false)
  const [revoking, setRevoking] = useState(false)
  const expired = isExpired(token)

  async function handleRevoke() {
    setRevoking(true)
    try {
      await onRevoke(token.id)
    } finally {
      setRevoking(false)
      setConfirming(false)
    }
  }

  return (
    <tr>
      <td>{token.name}</td>
      <td className="text-secondary">{formatDate(token.created_at)}</td>
      <td className="text-secondary">{formatDate(token.expires_at)}</td>
      <td className="text-secondary">
        {token.last_used_at ? formatDate(token.last_used_at) : 'Never used'}
      </td>
      <td>
        {expired ? (
          <span className="badge badge-warning">Expired</span>
        ) : (
          <span className="badge badge-ok">Active</span>
        )}
      </td>
      <td>
        {confirming ? (
          <span className="row gap-8">
            <button
              type="button"
              className="btn btn-danger btn-sm"
              onClick={handleRevoke}
              disabled={revoking}
            >
              {revoking ? 'Revoking…' : 'Confirm revoke'}
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => setConfirming(false)}
              disabled={revoking}
            >
              Cancel
            </button>
          </span>
        ) : (
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => setConfirming(true)}
          >
            Revoke
          </button>
        )}
      </td>
    </tr>
  )
}

function CreateTokenForm({ onCreated }: { onCreated: () => void }) {
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [expiresDays, setExpiresDays] = useState(90)
  const [redact, setRedact] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [newToken, setNewToken] = useState<string | null>(null)
  const tokenRef = useRef<HTMLInputElement>(null)

  function reset() {
    setName('')
    setExpiresDays(90)
    setRedact(false)
    setError(null)
    setNewToken(null)
    setOpen(false)
  }

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setError(null)
    if (!name.trim()) {
      setError('Token name is required.')
      return
    }
    setSaving(true)
    try {
      const result = await createApiToken({
        name: name.trim(),
        expires_days: expiresDays,
        redact_addresses: redact,
      })
      setNewToken(result.token)
      onCreated()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to create token.')
    } finally {
      setSaving(false)
    }
  }

  function copyToken() {
    if (tokenRef.current) {
      tokenRef.current.select()
      navigator.clipboard.writeText(tokenRef.current.value).catch(() => {})
    }
  }

  if (newToken) {
    return (
      <div className="card" role="alert" aria-live="polite">
        <h3 className="section-heading mb-8">Token created — copy it now</h3>
        <p className="muted-text mb-16">
          This token will not be shown again. Store it securely before closing this panel.
        </p>
        <p className="muted-text mb-8" style={{ fontStyle: 'italic', fontSize: '0.85em' }}>
          Warning: pasting this token into a hosted assistant (ChatGPT, Claude.ai, etc.) shares
          your portfolio data with that vendor. For private use, prefer the local stdio MCP path.
        </p>
        <div className="row gap-8 mb-16">
          <input
            ref={tokenRef}
            type="text"
            readOnly
            value={newToken}
            className="form-input"
            style={{ fontFamily: 'monospace', flex: 1 }}
            aria-label="New API token"
            onFocus={(e) => e.target.select()}
          />
          <button type="button" className="btn btn-secondary" onClick={copyToken}>
            Copy
          </button>
        </div>
        <button type="button" className="btn btn-primary" onClick={reset}>
          Done
        </button>
      </div>
    )
  }

  if (!open) {
    return (
      <button type="button" className="btn btn-primary" onClick={() => setOpen(true)}>
        Create token
      </button>
    )
  }

  return (
    <div className="card">
      <h3 className="section-heading mb-16">New API token</h3>
      <form onSubmit={handleSubmit} noValidate className="form-grid">
        <div className="form-group">
          <label htmlFor="token-name" className="form-label">
            Name
          </label>
          <input
            id="token-name"
            type="text"
            className="form-input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. home-cron, claude-desktop"
            maxLength={128}
            required
            autoFocus
          />
        </div>

        <div className="form-group">
          <label htmlFor="token-expires" className="form-label">
            Expiry (days)
          </label>
          <input
            id="token-expires"
            type="number"
            className="form-input"
            value={expiresDays}
            onChange={(e) => setExpiresDays(Number(e.target.value))}
            min={1}
            max={3650}
          />
        </div>

        <div className="form-group">
          <label className="row gap-8" style={{ cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={redact}
              onChange={(e) => setRedact(e.target.checked)}
            />
            <span>
              Redact wallet addresses{' '}
              <span className="muted-text">(replace with label + truncated hash)</span>
            </span>
          </label>
        </div>

        {error && <p className="text-danger">{error}</p>}

        <div className="row gap-8">
          <button type="submit" className="btn btn-primary" disabled={saving}>
            {saving ? 'Creating…' : 'Create'}
          </button>
          <button
            type="button"
            className="btn btn-ghost"
            onClick={reset}
            disabled={saving}
          >
            Cancel
          </button>
        </div>
      </form>
    </div>
  )
}

export default function ApiTokensPage() {
  const queryClient = useQueryClient()

  const tokensQuery = useQuery({
    queryKey: ['api-tokens'],
    queryFn: fetchApiTokens,
  })

  async function handleRevoke(id: string) {
    await revokeApiToken(id)
    queryClient.invalidateQueries({ queryKey: ['api-tokens'] })
  }

  function handleCreated() {
    queryClient.invalidateQueries({ queryKey: ['api-tokens'] })
  }

  const tokens = tokensQuery.data?.items ?? []

  return (
    <div className="page-stack">
      <section aria-label="API tokens" className="card">
        <h2 className="section-heading mb-8">API Tokens</h2>
        <p className="muted-text mb-16">
          Read-only Bearer tokens for scripts and local LLM clients. Tokens can only read
          portfolio, holdings, history, events, assets, news, and alerts — never your
          integrations, settings, or exports.
        </p>

        {tokensQuery.isPending && <p className="muted-text">Loading…</p>}
        {tokensQuery.isError && (
          <p className="text-danger">Failed to load tokens.</p>
        )}

        {!tokensQuery.isPending && tokens.length === 0 && (
          <p className="muted-text mb-16">
            No active tokens. Create one to connect a local LLM client — Claude Desktop,
            ChatGPT, or your own script — to this portfolio over its read-only API. A token
            grants read access to your entire portfolio, so treat it like a password.
          </p>
        )}

        {tokens.length > 0 && (
          <div className="table-container mb-16">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Created</th>
                  <th>Expires</th>
                  <th>Last used</th>
                  <th>Status</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {tokens.map((t) => (
                  <TokenRow key={t.id} token={t} onRevoke={handleRevoke} />
                ))}
              </tbody>
            </table>
          </div>
        )}

        <CreateTokenForm onCreated={handleCreated} />
      </section>
    </div>
  )
}
