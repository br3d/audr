import { useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation } from '@tanstack/react-query'
import { login } from '../api/client'
import { errorMessage } from '../api/errorMessage'
import { BrandLockup } from '../components/Logo'

interface Props {
  onSignIn: () => void
}

export default function SignInPage({ onSignIn }: Props) {
  const [password, setPassword] = useState('')

  const signInMutation = useMutation({
    mutationFn: () => login(password),
    onSuccess: onSignIn,
  })

  function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    signInMutation.mutate()
  }

  const submitting = signInMutation.isPending
  const error = signInMutation.error
    ? errorMessage(signInMutation.error, 'An unexpected error occurred. Please try again.')
    : null

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
