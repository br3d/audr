import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ApiError,
  assistantChat,
  fetchAssistantPresets,
  fetchPortfolio,
  deleteAllAssistantConversations,
  deleteAssistantConversation,
  getAssistantConversation,
  listAssistantConversations,
  renameAssistantConversation,
} from '../api/client'
import type { AssistantMode, AssistantPreset, AssistantStoredMessage } from '../api/client'
import { assistantIdFromHash, setAssistantHash } from '../routing'

// AUD-956/959: the assistant tab. Conversations are stored server-side
// (AUD-958): the list and the open transcript come from React Query, and the
// open conversation id lives in the URL hash (#/assistant/<id>) so a reload
// keeps it. Answers are rendered as plain text (white-space: pre-wrap) —
// never as HTML.

const POLL_MS = 3000
const LIST_KEY = ['assistant', 'conversations'] as const
const detailKey = (id: string) => ['assistant', 'conversation', id] as const

interface SendArgs {
  mode: AssistantMode
  /** Set when continuing a stored conversation. */
  conversationId: string | null
  /** The new user turn; empty for a quick action. */
  text: string
  /** AUD-962: a preset id from GET /assistant/presets (new conversation only). */
  preset?: string
  /** Asset id for presets with needs_asset. */
  assetRef?: string
  /** Symbol to append to the sidebar title of an asset preset. */
  assetSymbol?: string
}

function useElapsedSeconds(running: boolean): number {
  const [seconds, setSeconds] = useState(0)
  useEffect(() => {
    if (!running) return
    setSeconds(0)
    const started = Date.now()
    const id = window.setInterval(() => setSeconds(Math.floor((Date.now() - started) / 1000)), 1000)
    return () => window.clearInterval(id)
  }, [running])
  return seconds
}

function useSelectedId(): string | null {
  const [id, setId] = useState(() => assistantIdFromHash(window.location.hash))
  useEffect(() => {
    const onChange = () => setId(assistantIdFromHash(window.location.hash))
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])
  return id
}

function formatDuration(ms: number): string {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`
}

function formatDate(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toISOString().slice(0, 10)
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  async function copy() {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      setCopied(false)
    }
  }
  return (
    <button
      type="button"
      className="btn btn-secondary assistant-copy"
      data-testid="assistant-copy"
      onClick={copy}
    >
      {copied ? 'Copied' : 'Copy'}
    </button>
  )
}

function StoredTurn({
  msg,
  onRetry,
  busy,
  promptText,
}: {
  msg: AssistantStoredMessage
  onRetry: () => void
  busy: boolean
  /** AUD-969: instruction text of the preset that produced this reply, if any. */
  promptText?: string
}) {
  const [showPrompt, setShowPrompt] = useState(false)
  if (msg.role === 'user') {
    return (
      <div data-testid="user-message" className="assistant-message assistant-message-user">
        <strong>You:</strong>
        <div className="assistant-text" style={{ whiteSpace: 'pre-wrap' }}>
          {msg.content}
        </div>
      </div>
    )
  }
  // Pending turns are shown by the single "Analysing…" indicator.
  if (msg.status === 'pending') return null
  if (msg.status === 'error') {
    return (
      <div className="alert alert-error" role="alert" data-testid="assistant-error">
        <span>{msg.content || `The assistant could not answer (${msg.error_code ?? 'error'}).`}</span>{' '}
        <button
          type="button"
          className="btn btn-secondary"
          data-testid="assistant-retry"
          disabled={busy}
          onClick={onRetry}
        >
          Retry
        </button>
      </div>
    )
  }
  return (
    <div data-testid="assistant-message" className="assistant-message assistant-message-assistant">
      <strong>Assistant:</strong>
      <div className="assistant-text" style={{ whiteSpace: 'pre-wrap' }}>
        {msg.content}
      </div>
      <div className="assistant-message-foot">
        {msg.model && (
          <small className="assistant-caption" data-testid="assistant-caption">
            {msg.model} · data as of {msg.context_as_of ?? 'unknown'} · took{' '}
            {msg.elapsed_ms == null ? 'unknown' : formatDuration(msg.elapsed_ms)}
          </small>
        )}
        <CopyButton text={msg.content} />
        {promptText && (
          <button
            type="button"
            className="btn btn-secondary"
            data-testid="assistant-show-prompt"
            aria-expanded={showPrompt}
            onClick={() => setShowPrompt((v) => !v)}
          >
            {showPrompt ? 'Hide prompt' : 'Show prompt'}
          </button>
        )}
      </div>
      {promptText && showPrompt && (
        <div data-testid="assistant-prompt">
          <pre
            className="assistant-prompt-text"
            data-testid="assistant-prompt-text"
            style={{ whiteSpace: 'pre-wrap' }}
          >
            {promptText}
          </pre>
          <small className="assistant-caption" data-testid="assistant-prompt-note">
            + your portfolio/news data (not shown)
          </small>
        </div>
      )}
    </div>
  )
}

export default function AssistantPanel() {
  const qc = useQueryClient()
  const selectedId = useSelectedId()
  const [input, setInput] = useState('')
  const [optimistic, setOptimistic] = useState<string | null>(null)
  const [listOpen, setListOpen] = useState(false)
  const [renaming, setRenaming] = useState<{ id: string; title: string } | null>(null)
  const [confirmAll, setConfirmAll] = useState(false)
  const lastRequest = useRef<SendArgs | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  const list = useQuery({ queryKey: LIST_KEY, queryFn: listAssistantConversations })
  const detail = useQuery({
    queryKey: detailKey(selectedId ?? ''),
    queryFn: () => getAssistantConversation(selectedId!),
    enabled: selectedId !== null,
    // A reload mid-answer finds a pending row: keep re-fetching until it settles.
    refetchInterval: (query) =>
      query.state.data?.messages.some((m) => m.status === 'pending') ? POLL_MS : false,
  })

  const presets = useQuery({ queryKey: ['assistant', 'presets'], queryFn: fetchAssistantPresets })
  const presetList: AssistantPreset[] = presets.data ?? []
  const [pendingPreset, setPendingPreset] = useState<AssistantPreset | null>(null)
  const [assetQuery, setAssetQuery] = useState('')
  const portfolio = useQuery({
    queryKey: ['assistant', 'held-assets'],
    queryFn: () => fetchPortfolio(),
    enabled: pendingPreset?.needs_asset === true,
  })

  const mutation = useMutation({
    mutationFn: ({ mode, conversationId, text, preset, assetRef }: SendArgs) =>
      assistantChat({
        ...(preset ? { preset } : { mode }),
        ...(assetRef ? { asset_ref: assetRef } : {}),
        messages: text ? [{ role: 'user', content: text }] : [],
        ...(conversationId ? { conversation_id: conversationId } : {}),
      }),
    onSuccess: async (res, vars) => {
      if (vars.preset && vars.assetSymbol) {
        const label = presetList.find((p) => p.id === vars.preset)?.label ?? 'Analysis'
        try {
          await renameAssistantConversation(res.conversation_id, `${label} — ${vars.assetSymbol}`)
        } catch {
          // The title is cosmetic; the generic preset label stays.
        }
      }
      await Promise.all([
        qc.invalidateQueries({ queryKey: LIST_KEY }),
        qc.invalidateQueries({ queryKey: detailKey(res.conversation_id) }),
      ])
      setAssistantHash(res.conversation_id)
    },
    onSettled: () => setOptimistic(null),
  })

  const rename = useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) => renameAssistantConversation(id, title),
    onSuccess: async () => {
      setRenaming(null)
      await qc.invalidateQueries({ queryKey: LIST_KEY })
      if (selectedId) await qc.invalidateQueries({ queryKey: detailKey(selectedId) })
    },
  })

  const remove = useMutation({
    mutationFn: (id: string) => deleteAssistantConversation(id),
    onSuccess: async (_v, id) => {
      if (id === selectedId) setAssistantHash(null)
      qc.removeQueries({ queryKey: detailKey(id) })
      await qc.invalidateQueries({ queryKey: LIST_KEY })
    },
  })

  const removeAll = useMutation({
    mutationFn: deleteAllAssistantConversations,
    onSuccess: async () => {
      setConfirmAll(false)
      setAssistantHash(null)
      qc.removeQueries({ queryKey: ['assistant', 'conversation'] })
      await qc.invalidateQueries({ queryKey: LIST_KEY })
    },
  })

  const messages = selectedId ? (detail.data?.messages ?? []) : []
  const storedPending = messages.some((m) => m.status === 'pending')
  const sending = mutation.isPending
  const busy = sending || storedPending
  const elapsed = useElapsedSeconds(sending)
  const mode: AssistantMode = detail.data?.mode ?? 'chat'

  useEffect(() => {
    bottomRef.current?.scrollIntoView?.({ behavior: 'smooth' })
  }, [messages.length, busy, optimistic])

  function send(args: SendArgs) {
    if (busy) return
    lastRequest.current = args
    mutation.mutate(args)
  }

  function newChat() {
    setAssistantHash(null)
    setInput('')
    mutation.reset()
    setListOpen(false)
  }

  function pickPreset(p: AssistantPreset) {
    // A preset starts a new conversation. Asset presets wait for a chosen asset.
    if (p.needs_asset) {
      setPendingPreset(p)
      setAssetQuery('')
      return
    }
    setPendingPreset(null)
    setAssistantHash(null)
    mutation.reset()
    send({ mode: 'chat', conversationId: null, text: '', preset: p.id })
  }

  function pickAsset(p: AssistantPreset, assetId: string, symbol: string) {
    setPendingPreset(null)
    setAssistantHash(null)
    mutation.reset()
    send({
      mode: 'chat',
      conversationId: null,
      text: '',
      preset: p.id,
      assetRef: assetId,
      assetSymbol: symbol,
    })
  }

  const heldAssets = (() => {
    const seen = new Set<string>()
    const out: { id: string; symbol: string; name: string }[] = []
    for (const a of portfolio.data?.allocations ?? []) {
      if (seen.has(a.asset_id) || !a.included) continue
      seen.add(a.asset_id)
      out.push({ id: a.asset_id, symbol: a.symbol, name: a.name })
    }
    const needle = assetQuery.trim().toLowerCase()
    return out.filter((a) => !needle || a.symbol.toLowerCase().includes(needle))
  })()

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    const text = input.trim()
    if (!text || busy) return
    setOptimistic(text)
    setInput('')
    send({ mode: selectedId ? mode : 'chat', conversationId: selectedId, text })
  }

  function retryStored() {
    const lastUser = [...messages].reverse().find((m) => m.role === 'user')
    if (!selectedId || !lastUser) return
    send({ mode, conversationId: selectedId, text: lastUser.content })
  }

  function open(id: string) {
    mutation.reset()
    setAssistantHash(id)
    setListOpen(false)
  }

  const error = mutation.error
  const notConfigured =
    error instanceof ApiError && error.status === 409 && error.body?.error.code === 'llm_not_configured'
  const items = list.data?.items ?? []

  return (
    <section aria-label="AI assistant" data-testid="assistant-panel" className="card assistant-panel">
      <header className="card-header">
        <h2 className="card-title assistant-panel-title">AI assistant</h2>
        <button
          type="button"
          className="btn btn-secondary assistant-list-toggle"
          aria-expanded={listOpen}
          data-testid="assistant-list-toggle"
          onClick={() => setListOpen((v) => !v)}
        >
          Conversations
        </button>
      </header>

      <div className="assistant-layout">
        <aside
          aria-label="Conversations"
          data-testid="assistant-sidebar"
          className={`assistant-sidebar${listOpen ? ' is-open' : ''}`}
        >
          <button
            type="button"
            className="btn btn-primary"
            data-testid="assistant-new-chat"
            onClick={newChat}
          >
            New chat
          </button>
          {list.isError && (
            <div className="alert alert-error" role="alert" data-testid="assistant-list-error">
              {list.error.message}
            </div>
          )}
          {list.isSuccess && items.length === 0 && (
            <p className="assistant-empty" data-testid="assistant-list-empty">
              No saved conversations yet.
            </p>
          )}
          <ul className="assistant-conversations" data-testid="assistant-list">
            {items.map((c) => (
              <li
                key={c.id}
                data-testid="assistant-conversation"
                className={`assistant-conversation${c.id === selectedId ? ' is-selected' : ''}`}
              >
                {renaming?.id === c.id ? (
                  <form
                    className="assistant-rename"
                    onSubmit={(e) => {
                      e.preventDefault()
                      const title = renaming.title.trim()
                      if (title) rename.mutate({ id: c.id, title })
                    }}
                  >
                    <input
                      className="input-folio"
                      aria-label="Conversation title"
                      data-testid="assistant-rename-input"
                      maxLength={120}
                      value={renaming.title}
                      onChange={(e) => setRenaming({ id: c.id, title: e.target.value })}
                    />
                    <button type="submit" className="btn btn-primary" data-testid="assistant-rename-save">
                      Save
                    </button>
                    <button type="button" className="btn btn-secondary" onClick={() => setRenaming(null)}>
                      Cancel
                    </button>
                  </form>
                ) : (
                  <>
                    <button
                      type="button"
                      className="assistant-conversation-open"
                      data-testid="assistant-conversation-open"
                      aria-current={c.id === selectedId ? 'true' : undefined}
                      onClick={() => open(c.id)}
                    >
                      <span className="assistant-conversation-title">{c.title}</span>
                      <span className="assistant-conversation-meta">
                        <span className="pill" data-testid="assistant-mode-badge">
                          {c.mode}
                        </span>{' '}
                        {formatDate(c.created_at)}
                      </span>
                    </button>
                    <span className="assistant-conversation-actions">
                      <button
                        type="button"
                        className="btn btn-secondary"
                        aria-label={`Rename ${c.title}`}
                        data-testid="assistant-rename"
                        onClick={() => setRenaming({ id: c.id, title: c.title })}
                      >
                        Rename
                      </button>
                      <button
                        type="button"
                        className="btn btn-secondary"
                        aria-label={`Delete ${c.title}`}
                        data-testid="assistant-delete"
                        disabled={remove.isPending}
                        onClick={() => remove.mutate(c.id)}
                      >
                        Delete
                      </button>
                    </span>
                  </>
                )}
              </li>
            ))}
          </ul>
          {rename.isError && (
            <div className="alert alert-error" role="alert">
              {rename.error.message}
            </div>
          )}
          {items.length > 0 &&
            (confirmAll ? (
              <div
                role="alertdialog"
                aria-label="Delete all history"
                data-testid="assistant-confirm-all"
                className="assistant-confirm"
              >
                <p>Delete all saved conversations? This cannot be undone.</p>
                <button
                  type="button"
                  className="btn btn-danger"
                  data-testid="assistant-confirm-all-yes"
                  disabled={removeAll.isPending}
                  onClick={() => removeAll.mutate()}
                >
                  Delete everything
                </button>
                <button type="button" className="btn btn-secondary" onClick={() => setConfirmAll(false)}>
                  Cancel
                </button>
              </div>
            ) : (
              <button
                type="button"
                className="btn btn-secondary"
                data-testid="assistant-delete-all"
                onClick={() => setConfirmAll(true)}
              >
                Delete all history
              </button>
            ))}
        </aside>

        <div className="assistant-main">
          <div className="assistant-quick" data-testid="assistant-presets">
            {presets.isError && (
              <div className="alert alert-error" role="alert" data-testid="assistant-presets-error">
                Could not load the ready-made prompts: {presets.error.message}
              </div>
            )}
            {presets.isPending && <span className="assistant-empty">Loading prompts…</span>}
            {presetList.map((p) => (
              <button
                key={p.id}
                type="button"
                className={`btn btn-secondary${pendingPreset?.id === p.id ? ' is-active' : ''}`}
                disabled={busy}
                title={p.description}
                aria-label={`${p.label}: ${p.description}`}
                onClick={() => pickPreset(p)}
                data-testid={`assistant-preset-${p.id}`}
              >
                {p.label}
              </button>
            ))}
          </div>

          {pendingPreset && (
            <div className="assistant-asset-picker" data-testid="assistant-asset-picker">
              <label htmlFor="assistant-asset-search">Choose an asset for “{pendingPreset.label}”</label>
              <input
                id="assistant-asset-search"
                type="search"
                className="input-folio"
                placeholder="Search by symbol"
                autoComplete="off"
                value={assetQuery}
                onChange={(e) => setAssetQuery(e.target.value)}
                data-testid="assistant-asset-search"
              />
              {portfolio.isPending && <p className="assistant-empty">Loading assets…</p>}
              {portfolio.isError && (
                <div className="alert alert-error" role="alert" data-testid="assistant-asset-error">
                  {portfolio.error.message}
                </div>
              )}
              {portfolio.isSuccess && heldAssets.length === 0 && (
                <p className="assistant-empty" data-testid="assistant-asset-empty">
                  No held assets match.
                </p>
              )}
              <ul className="assistant-asset-list">
                {heldAssets.map((a) => (
                  <li key={a.id}>
                    <button
                      type="button"
                      className="btn btn-secondary"
                      disabled={busy}
                      title={a.name}
                      data-testid="assistant-asset-option"
                      onClick={() => pickAsset(pendingPreset, a.id, a.symbol)}
                    >
                      {a.symbol}
                    </button>
                  </li>
                ))}
              </ul>
              <button type="button" className="btn btn-secondary" onClick={() => setPendingPreset(null)}>
                Cancel
              </button>
            </div>
          )}

          <div
            role="log"
            aria-live="polite"
            aria-label="Conversation"
            data-testid="assistant-messages"
            className="assistant-messages"
          >
            {selectedId && detail.isPending && <p className="assistant-empty">Loading…</p>}
            {selectedId && detail.isError && (
              <div className="alert alert-error" role="alert" data-testid="assistant-detail-error">
                {detail.error instanceof ApiError && detail.error.status === 404
                  ? 'This conversation no longer exists.'
                  : detail.error.message}
              </div>
            )}
            {messages.map((msg, i) => (
              <StoredTurn
                key={msg.id}
                msg={msg}
                busy={busy}
                onRetry={retryStored}
                promptText={
                  i === 1 && msg.role === 'assistant' && messages[0].role === 'user'
                    ? presetList.find((p) => p.user_prompt === messages[0].content)?.prompt_text
                    : undefined
                }
              />
            ))}
            {optimistic !== null && (
              <div data-testid="user-message" className="assistant-message assistant-message-user">
                <strong>You:</strong>
                <div className="assistant-text" style={{ whiteSpace: 'pre-wrap' }}>
                  {optimistic}
                </div>
              </div>
            )}
            {busy && (
              <div
                role="status"
                data-testid="assistant-pending"
                className="assistant-message assistant-message-assistant assistant-typing"
              >
                {sending ? `Analysing… ${elapsed} s` : 'Analysing…'}
              </div>
            )}
            <div ref={bottomRef} />
          </div>

          {notConfigured && (
            <div className="empty-state" data-testid="assistant-not-configured">
              <p>
                <strong>LLM is not connected.</strong> Connect a model provider to use the assistant.
              </p>
              <a href="#/connections">Go to Settings → Connections</a>
            </div>
          )}

          {error && !notConfigured && (
            <div className="alert alert-error" role="alert" data-testid="assistant-send-error">
              <span>{error.message}</span>{' '}
              <button
                type="button"
                className="btn btn-secondary"
                data-testid="assistant-send-retry"
                onClick={() => lastRequest.current && send(lastRequest.current)}
              >
                Retry
              </button>
            </div>
          )}

          <form onSubmit={handleSubmit} aria-label="Send message" className="assistant-form">
            <label htmlFor="assistant-input" className="visually-hidden">
              Message
            </label>
            <input
              id="assistant-input"
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Ask a question about your portfolio or the news"
              disabled={busy}
              autoComplete="off"
              data-testid="assistant-input"
              className="input-folio"
            />
            <button
              type="submit"
              disabled={busy || !input.trim()}
              data-testid="assistant-send"
              className="btn btn-primary"
            >
              Send
            </button>
          </form>

          <p className="assistant-disclaimer" data-testid="assistant-disclaimer">
            Analysis is informational and not financial advice.
          </p>
        </div>
      </div>
    </section>
  )
}
