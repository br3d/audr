import { useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation } from '@tanstack/react-query'
import { changePassword } from '../../api/client'
import { errorMessage } from '../../api/errorMessage'

export default function PasswordSection() {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [validationError, setValidationError] = useState<string | null>(null)

  const changePasswordMutation = useMutation({
    mutationFn: () => changePassword(current, next),
    onSuccess: () => {
      setCurrent('')
      setNext('')
      setConfirm('')
    },
  })

  function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setValidationError(null)
    changePasswordMutation.reset()

    if (next.length < 12) {
      setValidationError('New password must be at least 12 characters.')
      return
    }
    if (next !== confirm) {
      setValidationError('New password and confirmation do not match.')
      return
    }

    changePasswordMutation.mutate()
  }

  const saving = changePasswordMutation.isPending
  const error =
    validationError ??
    (changePasswordMutation.error
      ? errorMessage(changePasswordMutation.error, 'Failed to change password.')
      : null)
  const msg = changePasswordMutation.isSuccess
    ? 'Password changed. All other sessions have been signed out. Sign in again to continue.'
    : null

  return (
    <section aria-label="Change password" className="card">
      <h2 className="section-heading mb-8">Change Password</h2>
      <p className="muted-text mb-16">
        Changing the password signs out all active sessions. You will need to sign in again.
      </p>
      <form onSubmit={handleSubmit} noValidate aria-label="Change password form" className="form-grid">
        <div className="form-group">
          <label htmlFor="current-password" className="form-label">Current password</label>
          <input
            id="current-password"
            type="password"
            className="input-folio"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            disabled={saving}
            required
            autoComplete="current-password"
          />
        </div>
        <div className="form-group">
          <label htmlFor="new-password" className="form-label">New password</label>
          <input
            id="new-password"
            type="password"
            className="input-folio"
            value={next}
            onChange={(e) => setNext(e.target.value)}
            disabled={saving}
            required
            autoComplete="new-password"
            aria-describedby="new-password-hint"
          />
          <p id="new-password-hint" className="form-hint">Minimum 12 characters.</p>
        </div>
        <div className="form-group">
          <label htmlFor="confirm-password" className="form-label">Confirm new password</label>
          <input
            id="confirm-password"
            type="password"
            className="input-folio"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            disabled={saving}
            required
            autoComplete="new-password"
          />
        </div>
        {error !== null && <p role="alert" className="alert alert-danger">{error}</p>}
        {msg !== null && <p role="status" className="alert alert-success">{msg}</p>}
        <button type="submit" className="btn btn-primary" disabled={saving}>
          {saving ? 'Changing password…' : 'Change password'}
        </button>
      </form>
    </section>
  )
}
