import { useState } from 'react'
import type { FormEvent } from 'react'
import {
  fetchAssets,
  addManualAsset,
  patchAsset,
  ApiError,
} from '../api/client'
import type { AssetItem } from '../api/client'
import { useInfiniteQuery, useQueryClient } from '@tanstack/react-query'
import { IconSearch, IconPlus, IconX } from '../components/Icons'
import { boolField, stringField, useHashQueryState } from '../routing'

const ASSETS_SCHEMA = {
  q: stringField(''),
  excluded: boolField(false),
  all: boolField(false),
}

function AddManualAssetForm({
  onAdded,
  onClose,
}: {
  onAdded: () => void
  onClose: () => void
}) {
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
      onClose()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'An unexpected error occurred.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="modal-overlay" role="dialog" aria-modal="true" aria-label="Add manual contract">
      <div className="modal-box">
        <div className="modal-header">
          <div className="modal-title">Add Manual Contract</div>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            <IconX width={16} height={16} />
          </button>
        </div>

        <form onSubmit={handleSubmit} noValidate>
          <div className="form-grid">
            <div className="form-group">
              <label htmlFor="contract-address" className="form-label">Contract address</label>
              <input
                id="contract-address"
                type="text"
                className="input-folio input-mono"
                value={contractAddress}
                onChange={(e) => setContractAddress(e.target.value)}
                placeholder="0x…"
                required
                disabled={submitting}
                autoComplete="off"
              />
            </div>
            <div className="form-group">
              <label htmlFor="decimals-override" className="form-label">
                Decimals override <span className="text-muted">(optional)</span>
              </label>
              <input
                id="decimals-override"
                type="number"
                className="input-folio"
                min={0}
                max={78}
                value={decimalsOverride}
                onChange={(e) => setDecimalsOverride(e.target.value)}
                disabled={submitting}
              />
            </div>
            <div className="form-group">
              <label htmlFor="symbol-override" className="form-label">
                Symbol override <span className="text-muted">(optional)</span>
              </label>
              <input
                id="symbol-override"
                type="text"
                className="input-folio"
                value={symbolOverride}
                onChange={(e) => setSymbolOverride(e.target.value)}
                disabled={submitting}
                autoComplete="off"
              />
            </div>
          </div>

          {error !== null && <p role="alert" className="alert alert-danger mt-12">{error}</p>}

          <div className="modal-footer">
            <button type="button" className="btn btn-ghost" onClick={onClose} disabled={submitting}>
              Cancel
            </button>
            <button type="submit" className="btn btn-primary" disabled={submitting}>
              {submitting ? 'Adding…' : 'Add contract'}
            </button>
          </div>
        </form>
      </div>
    </div>
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

  return (
    <tr>
      <td>
        <span className="fw-600">{asset.symbol}</span>
        {asset.excluded && (
          <span className="badge badge-neutral" style={{ marginLeft: 8 }}>excluded</span>
        )}
        {asset.has_metadata_conflict && (
          <span className="badge badge-warning" style={{ marginLeft: 8 }}>conflict</span>
        )}
      </td>
      <td className="text-secondary">{asset.name ?? '—'}</td>
      <td>
        <span className="badge badge-neutral" aria-label={`Kind: ${asset.kind}`}>
          {asset.kind}
        </span>
      </td>
      <td className="td-mono">
        {asset.contract_address ? (
          <span title={asset.contract_address}>
            {asset.contract_address.slice(0, 6)}…{asset.contract_address.slice(-4)}
          </span>
        ) : (
          <span className="text-muted">native</span>
        )}
      </td>
      <td>
        {asset.decimals === null ? (
          <span className="text-warning" role="note">unknown</span>
        ) : (
          <span className="text-secondary">{asset.decimals}</span>
        )}
      </td>
      <td>
        <div className="row gap-8">
          <button
            type="button"
            className={`btn btn-sm ${asset.excluded ? 'btn-secondary' : 'btn-ghost'}`}
            onClick={handleToggleExclude}
            disabled={saving}
            aria-label={asset.excluded ? `Include ${asset.symbol}` : `Exclude ${asset.symbol}`}
          >
            {asset.excluded ? 'Include' : 'Exclude'}
          </button>

          {asset.has_metadata_conflict && !editingDecimals && (
            <button
              type="button"
              className="btn btn-sm btn-secondary"
              onClick={() => {
                setDecimalsInput(String(asset.decimals ?? ''))
                setEditingDecimals(true)
              }}
              disabled={saving}
            >
              Resolve
            </button>
          )}
        </div>

        {editingDecimals && (
          <form onSubmit={handleSaveDecimals} aria-label="Resolve decimals" className="row mt-8">
            <input
              id={`decimals-${asset.id}`}
              type="number"
              min={0}
              max={78}
              value={decimalsInput}
              onChange={(e) => setDecimalsInput(e.target.value)}
              required
              disabled={saving}
              className="input-folio"
              style={{ width: 80 }}
              aria-label="Correct decimals"
            />
            <button type="submit" className="btn btn-sm btn-primary" disabled={saving}>
              {saving ? 'Saving…' : 'Confirm'}
            </button>
            <button
              type="button"
              className="btn btn-sm btn-ghost"
              onClick={() => setEditingDecimals(false)}
              disabled={saving}
            >
              Cancel
            </button>
          </form>
        )}

        {error !== null && <p role="alert" className="alert alert-danger mt-8">{error}</p>}
      </td>
    </tr>
  )
}

export default function AssetsPage() {
  const queryClient = useQueryClient()
  const [{ q: search, excluded: showExcluded, all: showAllCatalog }, updateQuery] =
    useHashQueryState('assets', ASSETS_SCHEMA)
  const [showAddForm, setShowAddForm] = useState(false)
  const setSearch = (value: string) => updateQuery({ q: value })
  const setShowExcluded = (value: boolean) => updateQuery({ excluded: value })
  const setShowAllCatalog = (value: boolean) => updateQuery({ all: value })

  // Discovery seeds a catalog row for every candidate token so balanceOf can be
  // checked against it — the catalog runs to hundreds of entries the owner has
  // never held. Default to the held-only view; "Show all catalog tokens" is the
  // escape hatch for pre-configuring a token before it has a balance.
  const heldFilter = showAllCatalog ? undefined : true

  const {
    data,
    error,
    isLoading,
    hasNextPage,
    fetchNextPage,
    isFetchingNextPage,
  } = useInfiniteQuery({
    queryKey: ['assets', showExcluded, showAllCatalog],
    queryFn: ({ pageParam }) =>
      fetchAssets({
        // Excluding an asset removes it from this list (AUD-447): the default
        // view is the assets that count towards the portfolio, and the
        // toggle below is how an excluded one is found again and included
        // back. Passing `undefined` here used to leave the row in place with
        // a badge, which contradicted the toggle's own "(N hidden)" label.
        excluded: showExcluded ? true : false,
        held: heldFilter,
        cursor: pageParam as string | undefined,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  })

  function invalidate() {
    void queryClient.invalidateQueries({ queryKey: ['assets'] })
    // Excluding here changes the portfolio total and the dashboard chart
    // (AUD-447) — drop their caches so the dashboard is not stale on return.
    void queryClient.invalidateQueries({ queryKey: ['portfolio'] })
    void queryClient.invalidateQueries({ queryKey: ['history'] })
  }

  // A just-added manual contract has no balance observation yet, so it is not
  // held and the default held-only view would hide it the moment the modal
  // closes — the add reads as a silent failure. Reveal the full catalog so the
  // new row is actually on screen.
  function onManualAssetAdded() {
    setShowAllCatalog(true)
    invalidate()
  }

  if (isLoading) {
    return <p aria-busy="true">Loading assets…</p>
  }

  if (error) {
    return (
      <p role="alert" className="alert alert-danger">
        Failed to load assets.{' '}
        {error instanceof ApiError ? error.message : 'Please try again.'}
      </p>
    )
  }

  const assets = data?.pages.flatMap((p) => p.items) ?? []
  const conflictCount = assets.filter((a) => a.has_metadata_conflict).length
  // Server-side count: the default page contains no excluded rows to count.
  const excludedCount = data?.pages[0]?.excluded_count ?? 0

  const filtered = search.trim()
    ? assets.filter((a) => {
        const q = search.toLowerCase()
        return (
          a.symbol.toLowerCase().includes(q) ||
          (a.name ?? '').toLowerCase().includes(q) ||
          (a.contract_address ?? '').toLowerCase().includes(q)
        )
      })
    : assets

  return (
    <div>
      {conflictCount > 0 && (
        <p role="note" className="alert alert-warning mb-16">
          {conflictCount} asset{conflictCount !== 1 ? 's have' : ' has'} a metadata
          conflict. Resolve to ensure correct quantities and values.
        </p>
      )}

      <div className="toolbar">
        <div className="search-bar">
          <IconSearch className="search-icon" />
          <input
            type="search"
            className="search-input"
            placeholder="Search assets…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label="Filter assets"
          />
        </div>

        <label className="toggle-row">
          <input
            type="checkbox"
            className="checkbox-folio"
            checked={showExcluded}
            onChange={(e) => setShowExcluded(e.target.checked)}
          />
          <span className="toggle-label">
            Show excluded
            {excludedCount > 0 && !showExcluded && (
              <span className="text-muted"> ({excludedCount} hidden)</span>
            )}
          </span>
        </label>

        <label className="toggle-row">
          <input
            type="checkbox"
            className="checkbox-folio"
            checked={showAllCatalog}
            onChange={(e) => setShowAllCatalog(e.target.checked)}
          />
          <span className="toggle-label">Show all catalog tokens</span>
        </label>

        <button
          type="button"
          className="btn btn-primary btn-sm"
          onClick={() => setShowAddForm(true)}
          style={{ marginLeft: 'auto' }}
        >
          <IconPlus width={14} height={14} />
          Add contract
        </button>
      </div>

      {assets.length === 0 ? (
        <div className="empty-state">
          <div className="empty-state-icon">🪙</div>
          <div className="empty-state-text">No assets found</div>
          <div className="empty-state-hint">
            {showExcluded
              ? 'No excluded assets.'
              : heldFilter
                ? 'No held assets yet. Run a balance scan to discover holdings, or show all catalog tokens to pre-configure one.'
                : 'No assets found.'}
          </div>
        </div>
      ) : (
        <>
          <div className="table-container">
            <table className="table-folio" aria-label="Assets">
              <thead>
                <tr>
                  <th scope="col">Symbol</th>
                  <th scope="col">Name</th>
                  <th scope="col">Type</th>
                  <th scope="col">Contract</th>
                  <th scope="col">Decimals</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((a) => (
                  <AssetRow key={a.id} asset={a} onChanged={invalidate} />
                ))}
              </tbody>
            </table>
          </div>
          {hasNextPage && (
            <div className="mt-16" style={{ textAlign: 'center' }}>
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => void fetchNextPage()}
                disabled={isFetchingNextPage}
              >
                {isFetchingNextPage ? 'Loading…' : 'Load more'}
              </button>
            </div>
          )}
        </>
      )}

      {showAddForm && (
        <AddManualAssetForm
          onAdded={onManualAssetAdded}
          onClose={() => setShowAddForm(false)}
        />
      )}
    </div>
  )
}
