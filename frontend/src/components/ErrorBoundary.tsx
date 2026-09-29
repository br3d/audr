import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  hasError: boolean
  message: string | null
}

export default class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props)
    this.state = { hasError: false, message: null }
  }

  static getDerivedStateFromError(error: unknown): State {
    const message = error instanceof Error ? error.message : String(error)
    return { hasError: true, message }
  }

  override componentDidCatch(error: unknown, info: ErrorInfo) {
    console.error('Uncaught render error', error, info.componentStack)
  }

  handleReload = () => {
    window.location.reload()
  }

  override render() {
    if (this.state.hasError) {
      return (
        <div
          role="alert"
          style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            minHeight: '100vh',
            padding: '2rem',
            textAlign: 'center',
            gap: '1rem',
          }}
        >
          <p style={{ fontSize: '1.1rem', fontWeight: 600 }}>
            Something went wrong.
          </p>
          {this.state.message && (
            <p style={{ fontSize: '0.875rem', color: 'var(--color-muted, #888)' }}>
              {this.state.message}
            </p>
          )}
          <button
            type="button"
            className="btn btn-primary"
            onClick={this.handleReload}
          >
            Reload page
          </button>
        </div>
      )
    }

    return this.props.children
  }
}
