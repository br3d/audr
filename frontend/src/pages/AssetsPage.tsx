import { useState } from 'react'
import type { FormEvent } from 'react'
import {
  fetchAssets,
  addManualAsset,
  patchAsset,
  ApiError,
} from '../api/client'
import type { AssetItem } from '../api/client'
import { useQuery, useQueryClient } from '@tanstack/react-query'

function AddManualAssetForm({ onAdded }: { onAdded: () => void }) {
  const [contractAddress, setContractAddress] = useState('')
  const [decimalsOverride, setDecimalsOverride] = useState('')
  const [symbolOverride, setSymbolOverride] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setError(null)
    const address = contractAddress.trim()
    if (!address) {
      setError('Contract address is required.')
      return
    }
    setSubmitting(true)
    try {
      await addManualAsset({
        contract_address: address,
        decimals_override: decimalsOverride ? Number(decimalsOverride) : undefined,
        symbol_override: symbolOverride.trim() || undefined,
      })
      setContractAddress('')
      setDecimalsOverride('')
      setSymbolOverride('')
      onAdded()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'An unexpected error occurred.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} noValidate aria-label="Add manual contract">
      <h2>Add manual contract</h2>
      <div>
        <label htmlFor="contract-address">Contract address</label>
        <input
          id="contract-address"
          type="text"
          value={contractAddress}
          onChange={(e) => setContractAddress(e.target.value)}
          placeholder="0x…"
          required
          disabled={submitting}
          autoComplete="off"
        />
      </div>
      <div>
        <label htmlFor="decimals-override">Decimals override (optional)</label>
        <input
          id="decimals-override"
          type="number"
          min={0}
          max={78}
          value={decimalsOverride}
          onChange={(e) => setDecimalsOverride(e.target.value)}
          disabled={submitting}
        />
      </div>
      <div>
        <label htmlFor="symbol-override">Symbol override (optional)</label>
        <input
          id="symbol-override"
          type="text"
          value={symbolOverride}
          onChange={(e) => setSymbolOverride(e.target.value)}
          disabled={submitting}
          autoComplete="off"
        />
      </div>
      {error !== null && <p role="alert">{error}</p>}
      <button type="submit" disabled={submitting}>
        {submitting ? 'Adding…' : 'Add contract'}
      </button>
    </form>
  )
}

function AssetRow({
  asset,
  onChanged,
}: {
  asset: AssetItem
  onChanged: () => void
}) {
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [decimalsInput, setDecimalsInput] = useState('')
  const [editingDecimals, setEditingDecimals] = useState(false)

  async function handleToggleExclude() {
    setError(null)
    setSaving(true)
    try {
      await patchAsset(asset.id, { excluded: !asset.excluded })
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to update exclusion.')
    } finally {
      setSaving(false)
    }
  }

  async function handleSaveDecimals(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setError(null)
    setSaving(true)
    try {
      await patchAsset(asset.id, {
        decimals_override: Number(decimalsInput),
        confirm_metadata_override: true,
      })
      setEditingDecimals(false)
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to save decimals.')
    } finally {
      setSaving(false)
    }
  }

  const unpricedNote =
    asset.decimals === null
      ? 'Unpriced: decimals unknown — raw balance shown, no value computed'
      : null

  return (
    <li>
      <div>
        <strong>{asset.symbol}</strong>
        {asset.name && <> — {asset.name}</>}
        {' '}
        <span aria-label={`Kind: ${asset.kind}`}>({asset.kind})</span>
        {asset.excluded && (
          <> <span aria-label="Excluded from totals">[excluded]</span></>
        )}
        {asset.contract_address && (
          <>
            {' '}
            <code>{asset.contract_address}</code>
          </>
        )}
      </div>

      {unpricedNote !== null && (
        <p role="note">{unpricedNote}</p>
      )}

      {asset.has_metadata_conflict && (
        <div role="note">
          <strong>Metadata conflict detected.</strong>{' '}
          On-chain and catalog decimals disagree — quantities and values may be
          incorrect until resolved.{' '}
          {editingDecimals ? (
            <form onSubmit={handleSaveDecimals} aria-label="Resolve decimals">
              <label htmlFor={`decimals-${asset.id}`}>Correct decimals</label>
              <input
                id={`decimals-${asset.id}`}
                type="number"
                min={0}
                max={78}
                value={decimalsInput}
                onChange={(e) => setDecimalsInput(e.target.value)}
                required
                disabled={saving}
              />
              <button type="submit" disabled={saving}>
                {saving ? 'Saving…' : 'Confirm'}
              </button>
              <button type="button" onClick={() => setEditingDecimals(false)} disabled={saving}>
                Cancel
              </button>
            </form>
          ) : (
            <button
              type="button"
              onClick={() => {
                setDecimalsInput(String(asset.decimals ?? ''))
                setEditingDecimals(true)
              }}
              disabled={saving}
            >
              Resolve decimals
            </button>
          )}
        </div>
      )}

      <div>
        <small>Source: {asset.metadata_source}</small>
        {' '}
        <button
          type="button"
          onClick={handleToggleExclude}
          disabled={saving}
          aria-label={asset.excluded ? `Include ${asset.symbol}` : `Exclude ${asset.symbol}`}
        >
          {asset.excluded ? 'Include' : 'Exclude'}
        </button>
      </div>

      {error !== null && <p role="alert">{error}</p>}
    </li>
  )
}

export default function AssetsPage() {
  const queryClient = useQueryClient()
  const [showExcluded, setShowExcluded] = useState(false)

  const { data, error, isLoading } = useQuery({
    queryKey: ['assets', showExcluded],
    queryFn: () => fetchAssets(showExcluded ? true : undefined),
  })

  function invalidate() {
    void queryClient.invalidateQueries({ queryKey: ['assets'] })
  }

  if (isLoading) {
    return <p>Loading assets…</p>
  }

  if (error) {
    return (
      <p role="alert">
        Failed to load assets.{' '}
        {error instanceof ApiError ? error.message : 'Please try again.'}
      </p>
    )
  }

  const assets = data?.items ?? []
  const conflictCount = assets.filter((a) => a.has_metadata_conflict).length
  const excludedCount = data?.items.filter((a) => a.excluded).length ?? 0

  return (
    <main>
      <h1>Assets</h1>
      <p>
        Holdings, contract identities, quantities and exclusions. Catalog
        metadata describes tokens but is not a safety endorsement.
      </p>

      <AddManualAssetForm onAdded={invalidate} />

      <div>
        <label>
          <input
            type="checkbox"
            checked={showExcluded}
            onChange={(e) => setShowExcluded(e.target.checked)}
          />
          {' Show excluded assets'}
          {excludedCount > 0 && !showExcluded && (
            <> ({excludedCount} hidden)</>
          )}
        </label>
      </div>

      {conflictCount > 0 && (
        <p role="note">
          {conflictCount} asset{conflictCount !== 1 ? 's have' : ' has'} a metadata
          conflict. Resolve to ensure correct quantities and values.
        </p>
      )}

      {assets.length === 0 ? (
        <p>{showExcluded ? 'No excluded assets.' : 'No assets found.'}</p>
      ) : (
        <ul aria-label="Assets">
          {assets.map((a) => (
            <AssetRow key={a.id} asset={a} onChanged={invalidate} />
          ))}
        </ul>
      )}
    </main>
  )
}
