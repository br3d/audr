import { useEffect, useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { ApiError, assistantChat } from '../api/client'
import type { AssistantChatResponse, AssistantMode } from '../api/client'

// AUD-956: talks to POST /api/v1/assistant/chat (AUD-955). The conversation
// lives only in component state; the server stores nothing. The answer is
// rendered as plain text (white-space: pre-wrap) — never as HTML.

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  /** Set on assistant turns: the response metadata shown as a caption. */
  meta?: AssistantChatResponse
}

interface SendArgs {
  mode: AssistantMode
  messages: ChatMessage[]
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

function formatDuration(ms: number): string {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`
}

export default function AssistantPanel() {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const lastRequest = useRef<SendArgs | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  const mutation = useMutation({
    mutationFn: ({ mode, messages: history }: SendArgs) =>
      assistantChat({ mode, messages: history.map(({ role, content }) => ({ role, content })) }),
    onSuccess: (res, vars) => {
      setMessages([...vars.messages, { role: 'assistant', content: res.reply, meta: res }])
    },
  })

  const pending = mutation.isPending
  const elapsed = useElapsedSeconds(pending)

  useEffect(() => {
    bottomRef.current?.scrollIntoView?.({ behavior: 'smooth' })
  }, [messages, pending])

  function send(args: SendArgs) {
    if (pending) return
    lastRequest.current = args
    mutation.mutate(args)
  }

  function quickAction(mode: 'portfolio' | 'news') {
    // A quick action starts a fresh analysis; earlier turns are dropped.
    setMessages([])
    send({ mode, messages: [] })
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    const text = input.trim()
    if (!text || pending) return
    const next: ChatMessage[] = [...messages, { role: 'user', content: text }]
    setMessages(next)
    setInput('')
    send({ mode: 'chat', messages: next })
  }

  const error = mutation.error
  const notConfigured =
    error instanceof ApiError && error.status === 409 && error.body?.error.code === 'llm_not_configured'

  return (
    <section aria-label="AI assistant" data-testid="assistant-panel" className="card assistant-panel">
      <header className="card-header">
        <h2 className="card-title assistant-panel-title">AI assistant</h2>
      </header>

      <div className="assistant-quick">
        <button
          type="button"
          className="btn btn-secondary"
          disabled={pending}
          onClick={() => quickAction('portfolio')}
          data-testid="assistant-quick-portfolio"
        >
          Analyse portfolio
        </button>
        <button
          type="button"
          className="btn btn-secondary"
          disabled={pending}
          onClick={() => quickAction('news')}
          data-testid="assistant-quick-news"
        >
          What matters in the news
        </button>
      </div>

      <div
        role="log"
        aria-live="polite"
        aria-label="Conversation"
        data-testid="assistant-messages"
        className="assistant-messages"
      >
        {messages.map((msg, i) => (
          <div
            key={i}
            data-testid={msg.role === 'user' ? 'user-message' : 'assistant-message'}
            className={`assistant-message assistant-message-${msg.role}`}
          >
            <strong>{msg.role === 'user' ? 'You' : 'Assistant'}:</strong>
            <div className="assistant-text" style={{ whiteSpace: 'pre-wrap' }}>
              {msg.content}
            </div>
            {msg.meta && (
              <small className="assistant-caption" data-testid="assistant-caption">
                {msg.meta.model} · data as of {msg.meta.context_as_of} · took{' '}
                {formatDuration(msg.meta.elapsed_ms)}
              </small>
            )}
          </div>
        ))}
        {pending && (
          <div
            role="status"
            data-testid="assistant-pending"
            className="assistant-message assistant-message-assistant assistant-typing"
          >
            Analysing… {elapsed} s
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
        <div className="alert alert-error" role="alert" data-testid="assistant-error">
          <span>{error.message}</span>{' '}
          <button
            type="button"
            className="btn btn-secondary"
            data-testid="assistant-retry"
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
          disabled={pending}
          autoComplete="off"
          data-testid="assistant-input"
          className="input-folio"
        />
        <button
          type="submit"
          disabled={pending || !input.trim()}
          data-testid="assistant-send"
          className="btn btn-primary"
        >
          Send
        </button>
      </form>

      <p className="assistant-disclaimer" data-testid="assistant-disclaimer">
        Analysis is informational and not financial advice.
      </p>
    </section>
  )
}
