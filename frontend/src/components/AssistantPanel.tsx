import { useState, useRef, useEffect } from 'react'

export interface ChatMessage {
  role: 'user' | 'assistant'
  text: string
}

// TODO(AUD-302): replace stub responses with POST /api/v1/assistant/chat
// When the backend LLM endpoint is ready, call it here instead of pickStubResponse()
// and remove the STUB_RESPONSES array. The ChatMessage shape and component interface
// are designed to require no UI changes when the real endpoint is wired in.

const STUB_RESPONSES = [
  'Your ETH holdings look healthy. Total balance across all wallets was last scanned recently.',
  'I can help you review your portfolio. Try asking about a specific token or wallet.',
  'AI assistant is in demo mode. Real LLM responses will be available in a future release.',
  'Portfolio analytics, wallet summaries, and token insights are coming with the full AI integration.',
  'I notice you have some holdings that may not have fresh price data. Check the Connections page to configure a quote provider.',
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
      text: 'Hello! I am your portfolio assistant (demo mode). Ask me anything about your holdings.',
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
          AI Assistant <span aria-label="demo badge" className="badge badge-info">demo</span>
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
          <div aria-label="Assistant is typing" data-testid="typing-indicator" className="assistant-message assistant-message-assistant assistant-typing">
            Assistant is typing…
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
          placeholder="Ask about your portfolio…"
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
