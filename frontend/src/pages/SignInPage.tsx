import { useState } from 'react'
import type { FormEvent } from 'react'
import { login, ApiError } from '../api/client'
import { BrandLockup } from '../components/Logo'

interface Props {
  onSignIn: () => void
}

export default function SignInPage({ onSignIn }: Props) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await login(password)
      onSignIn()
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message)
      } else {
        setError('An unexpected error occurred. Please try again.')
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="auth-screen">
      <div className="auth-card">
        <div className="auth-logo">
          <BrandLockup />
        </div>
        <h1 className="auth-title">Sign in</h1>
        <p className="auth-subtitle">Enter your portfolio password to continue.</p>
        <form onSubmit={handleSubmit} noValidate className="form-grid">
          <div className="form-group">
            <label htmlFor="password" className="form-label">Password</label>
            <input
              id="password"
              type="password"
              className="input-folio"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              autoComplete="current-password"
              disabled={submitting}
            />
          </div>
          {error !== null && (
            <p role="alert" className="alert alert-danger">
              {error}
            </p>
          )}
          <button type="submit" className="btn btn-primary" disabled={submitting}>
            {submitting ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  )
}
