import { useMutation } from '@tanstack/react-query'
import { deleteWallet } from '../../api/client'
import type { WalletItem } from '../../api/client'
import { errorMessage } from '../../api/errorMessage'
import { IconX } from '../Icons'

export default function DeleteWalletModal({
  wallet,
  onDeleted,
  onClose,
}: {
  wallet: WalletItem
  onDeleted: (summary: string) => void
  onClose: () => void
}) {
  const deleteMutation = useMutation({
    mutationFn: () => deleteWallet(wallet.id),
    onSuccess: (result) => {
      const rows = Object.values(result.deleted).reduce((a, b) => a + b, 0)
      onDeleted(
        `Removed ${wallet.label || wallet.address} and ${rows} related record${
          rows === 1 ? '' : 's'
        }.`,
      )
      onClose()
    },
  })

  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="Delete tracked address"
    >
      <div className="modal-box">
        <div className="modal-header">
          <div className="modal-title">Delete Tracked Address</div>
          <button
            type="button"
            className="modal-close"
            onClick={onClose}
            aria-label="Close"
            disabled={deleteMutation.isPending}
          >
            <IconX width={16} height={16} />
          </button>
        </div>

        <p>
          <strong>{wallet.label || '(no label)'}</strong>
          <br />
          <span className="wallet-address">{wallet.address}</span>
        </p>
        <p className="muted-text mt-8">
          This permanently removes the address and everything derived from it —
          balance observations, discovered token coverage, indexed on-chain events
          and its share of valuation history. It cannot be undone.
        </p>
        <p className="muted-text mt-8">
          If you only want to pause scanning and keep the history, use{' '}
          <strong>Stop tracking</strong> instead.
        </p>

        {deleteMutation.error && (
          <p role="alert" className="alert alert-danger mt-12">
            {errorMessage(deleteMutation.error, 'Failed to delete address.')}
          </p>
        )}

        <div className="modal-footer">
          <button
            type="button"
            className="btn btn-ghost"
            onClick={onClose}
            disabled={deleteMutation.isPending}
          >
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-danger"
            onClick={() => deleteMutation.mutate()}
            disabled={deleteMutation.isPending}
          >
            {deleteMutation.isPending ? 'Deleting…' : 'Delete permanently'}
          </button>
        </div>
      </div>
    </div>
  )
}
