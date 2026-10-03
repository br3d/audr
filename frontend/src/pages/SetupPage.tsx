import { useState } from 'react'
import type { FormEvent } from 'react'
import { setup, ApiError } from '../api/client'
import { BrandLockup } from '../components/Logo'

interface Props {
  onSetupComplete: () => void
}

const MIN_PASSWORD_LENGTH = 12
const MAX_PASSWORD_LENGTH = 128

export default function SetupPage({ onSetupComplete }: Props) {
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setError(null)

    if (password !== confirm) {
      setError('Passwords do not match.')
      return
    }
    if (password.length < MIN_PASSWORD_LENGTH) {
      setError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`)
      return
    }
    if (password.length > MAX_PASSWORD_LENGTH) {
      setError(`Password must be at most ${MAX_PASSWORD_LENGTH} characters.`)
      return
    }

    setSubmitting(true)
    try {
      await setup(password)
      onSetupComplete()
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
        <h1 className="auth-title">Set up audr</h1>
        <p className="auth-subtitle">
          Choose a password to protect your portfolio. You will use it to sign in.
        </p>
        <form onSubmit={handleSubmit} noValidate className="form-grid">
          <div className="form-group">
            <label htmlFor="password" className="form-label">Password</label>
            <input
              id="password"
              type="password"
              className="input-folio"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              minLength={MIN_PASSWORD_LENGTH}
              maxLength={MAX_PASSWORD_LENGTH}
              required
              autoComplete="new-password"
              disabled={submitting}
            />
          </div>
          <div className="form-group">
            <label htmlFor="confirm" className="form-label">Confirm password</label>
            <input
              id="confirm"
              type="password"
              className="input-folio"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              minLength={MIN_PASSWORD_LENGTH}
              maxLength={MAX_PASSWORD_LENGTH}
              required
              autoComplete="new-password"
              disabled={submitting}
            />
          </div>
          {error !== null && (
            <p role="alert" className="alert alert-danger">
              {error}
            </p>
          )}
          <button type="submit" className="btn btn-primary" disabled={submitting}>
            {submitting ? 'Setting up…' : 'Set up'}
          </button>
        </form>
      </div>
    </div>
  )
}
