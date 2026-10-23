import { useState } from 'react'
import type { FormEvent } from 'react'
import { changePassword, ApiError } from '../../api/client'

export default function PasswordSection() {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setError(null)
    setMsg(null)

    if (next.length < 12) {
      setError('New password must be at least 12 characters.')
      return
    }
    if (next !== confirm) {
      setError('New password and confirmation do not match.')
      return
    }

    setSaving(true)
    try {
      await changePassword(current, next)
      setMsg(
        'Password changed. All other sessions have been signed out. Sign in again to continue.',
      )
      setCurrent('')
      setNext('')
      setConfirm('')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to change password.')
    } finally {
      setSaving(false)
    }
  }

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
