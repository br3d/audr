import { useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation } from '@tanstack/react-query'
import { addWallet } from '../../api/client'
import { errorMessage } from '../../api/errorMessage'
import { IconX } from '../Icons'

const ETH_ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/

function validateEthAddress(value: string): string | null {
  const trimmed = value.trim()
  if (!trimmed) return 'Wallet address is required.'
  if (!ETH_ADDRESS_RE.test(trimmed))
    return 'Enter a valid Ethereum address (0x followed by 40 hex characters).'
  return null
}

export default function AddWalletModal({
  onAdded,
  onClose,
}: {
  onAdded: () => void
  onClose: () => void
}) {
  const [address, setAddress] = useState('')
  const [label, setLabel] = useState('')
  const [validationError, setValidationError] = useState<string | null>(null)

  const addMutation = useMutation({
    mutationFn: () =>
      addWallet({
        address: address.trim(),
        label: label.trim() || undefined,
        chain_id: 1,
      }),
    onSuccess: () => {
      onAdded()
      onClose()
    },
  })

  function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setValidationError(null)
    const validation = validateEthAddress(address)
    if (validation) {
      setValidationError(validation)
      return
    }
    addMutation.mutate()
  }

  const submitting = addMutation.isPending
  const error =
    validationError ??
    (addMutation.error ? errorMessage(addMutation.error, 'An unexpected error occurred.') : null)

  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="Add tracked address"
    >
      <div className="modal-box">
        <div className="modal-header">
          <div className="modal-title">Add Tracked Address</div>
          <button
            type="button"
            className="modal-close"
            onClick={onClose}
            aria-label="Close"
          >
            <IconX width={16} height={16} />
          </button>
        </div>

        <form onSubmit={handleSubmit} noValidate>
          <div className="form-grid">
            <div className="form-group">
              <label htmlFor="wallet-address" className="form-label">
                Ethereum address
              </label>
              <input
                id="wallet-address"
                type="text"
                className="input-folio input-mono"
                value={address}
                onChange={(e) => setAddress(e.target.value)}
                placeholder="0x…"
                required
                disabled={submitting}
                autoComplete="off"
                aria-describedby="wallet-address-hint"
              />
              <p id="wallet-address-hint" className="form-hint">
                Any valid public Ethereum mainnet address (0x + 40 hex characters). No
                proof of control is required or verified.
              </p>
            </div>
            <div className="form-group">
              <label htmlFor="wallet-label" className="form-label">
                Label <span className="text-muted">(optional)</span>
              </label>
              <input
                id="wallet-label"
                type="text"
                className="input-folio"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                disabled={submitting}
                autoComplete="off"
              />
            </div>
          </div>

          {error !== null && (
            <p role="alert" className="alert alert-danger mt-12">
              {error}
            </p>
          )}

          <div className="modal-footer">
            <button
              type="button"
              className="btn btn-ghost"
              onClick={onClose}
              disabled={submitting}
            >
              Cancel
            </button>
            <button type="submit" className="btn btn-primary" disabled={submitting}>
              {submitting ? 'Adding…' : 'Add tracked address'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
