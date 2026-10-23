import { useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation } from '@tanstack/react-query'
import { setup } from '../api/client'
import { errorMessage } from '../api/errorMessage'
import { BrandLockup } from '../components/Logo'

interface Props {
  onSetupComplete: () => void
}

const MIN_PASSWORD_LENGTH = 12
const MAX_PASSWORD_LENGTH = 128

export default function SetupPage({ onSetupComplete }: Props) {
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [validationError, setValidationError] = useState<string | null>(null)

  const setupMutation = useMutation({
    mutationFn: () => setup(password),
    onSuccess: onSetupComplete,
  })

  function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setValidationError(null)

    if (password !== confirm) {
      setValidationError('Passwords do not match.')
      return
    }
    if (password.length < MIN_PASSWORD_LENGTH) {
      setValidationError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`)
      return
    }
    if (password.length > MAX_PASSWORD_LENGTH) {
      setValidationError(`Password must be at most ${MAX_PASSWORD_LENGTH} characters.`)
      return
    }

    setupMutation.mutate()
  }

  const submitting = setupMutation.isPending
  const error =
    validationError ??
    (setupMutation.error
      ? errorMessage(setupMutation.error, 'An unexpected error occurred. Please try again.')
      : null)

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
