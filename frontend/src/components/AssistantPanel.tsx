import { useState, useRef, useEffect } from 'react'

export interface ChatMessage {
  role: 'user' | 'assistant'
  text: string
}

// TODO(AUD-302): replace stub responses with POST /api/v1/assistant/chat
// When the backend LLM endpoint is ready, call it here instead of pickStubResponse()
// and remove the STUB_RESPONSES array. The ChatMessage shape and component interface
// are designed to require no UI changes when the real endpoint is wired in.
//
// AUD-436: these lines must never state anything about the operator's actual
// holdings. The earlier set included "Your ETH holdings look healthy" and a
// claim about stale price data — assertions made without reading a single
// balance, which is exactly what made this prototype feel dishonest. Keep
// every line self-describing: it may explain what the finished assistant
// would do, never what the portfolio currently is.

const STUB_RESPONSES = [
  'This is sample text, not an answer — the prototype has no access to your wallets or balances.',
  'When the assistant is implemented, a question like that will be answered from your own indexed data.',
  'Still a prototype: there is no model behind this box, only a fixed list of phrases played in order.',
  'Planned scope: portfolio summaries, per-token questions, and history explanations — none of it live yet.',
]

let _stubIndex = 0
function pickStubResponse(): string {
  const response = STUB_RESPONSES[_stubIndex % STUB_RESPONSES.length]
  _stubIndex++
  return response
}

interface AssistantPanelProps {
  onClose?: () => void
}

export default function AssistantPanel({ onClose }: AssistantPanelProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      role: 'assistant',
      text:
        'This is a non-functional prototype of the portfolio assistant. Anything you send gets ' +
        'a fixed sample reply — your holdings are never read or analysed.',
    },
  ])
  const [input, setInput] = useState('')
  const [isTyping, setIsTyping] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView?.({ behavior: 'smooth' })
  }, [messages])

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    const text = input.trim()
    if (!text || isTyping) return

    const userMsg: ChatMessage = { role: 'user', text }
    setMessages((prev) => [...prev, userMsg])
    setInput('')
    setIsTyping(true)

    // Simulate async response delay
    setTimeout(() => {
      const reply: ChatMessage = { role: 'assistant', text: pickStubResponse() }
      setMessages((prev) => [...prev, reply])
      setIsTyping(false)
    }, 600)
  }

  return (
    <section aria-label="AI Assistant" data-testid="assistant-panel" className="card assistant-panel">
      <header className="card-header">
        <h2 className="card-title assistant-panel-title">
          AI Assistant{' '}
          <span aria-label="prototype badge" className="badge badge-warning">
            prototype
          </span>
        </h2>
        {onClose && (
          <button
            type="button"
            className="btn btn-ghost btn-icon"
            onClick={onClose}
            aria-label="Close assistant panel"
          >
            ×
          </button>
        )}
      </header>

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
            aria-label={msg.role === 'user' ? 'You' : 'Assistant'}
            className={`assistant-message assistant-message-${msg.role}`}
          >
            <strong>{msg.role === 'user' ? 'You' : 'Assistant'}:</strong> {msg.text}
          </div>
        ))}
        {isTyping && (
          // AUD-436: the delay demonstrates the pending state of the finished
          // interface, so it stays — but it is labelled as simulated rather
          // than dressed up as an assistant that is thinking.
          <div aria-label="Simulated reply pending" data-testid="typing-indicator" className="assistant-message assistant-message-assistant assistant-typing">
            Simulated reply…
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      <form onSubmit={handleSubmit} aria-label="Send message" className="assistant-form">
        <label htmlFor="assistant-input" className="visually-hidden">
          Message
        </label>
        <input
          id="assistant-input"
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Type anything — the reply is sample text"
          disabled={isTyping}
          autoComplete="off"
          data-testid="assistant-input"
          className="input-folio"
        />
        <button
          type="submit"
          disabled={isTyping || !input.trim()}
          data-testid="assistant-send"
          className="btn btn-primary"
        >
          Send
        </button>
      </form>
    </section>
  )
}
