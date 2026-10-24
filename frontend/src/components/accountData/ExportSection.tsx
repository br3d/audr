import { useState, useRef } from 'react'
import { exportData } from '../../api/client'
import { errorMessage } from '../../api/errorMessage'
import type { ExportKind, ExportFormat } from '../../api/client'

type ExportState = 'idle' | 'streaming' | 'done' | 'error'

export default function ExportSection() {
  const [kind, setKind] = useState<ExportKind>('portfolio')
  const [format, setFormat] = useState<ExportFormat>('json')
  const [exportState, setExportState] = useState<ExportState>('idle')
  const [progress, setProgress] = useState<string | null>(null)
  const [exportError, setExportError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  async function handleExport() {
    setExportState('streaming')
    setProgress(null)
    setExportError(null)

    const controller = new AbortController()
    abortRef.current = controller

    try {
      const response = await exportData(kind, format, { signal: controller.signal })

      const disposition = response.headers.get('Content-Disposition') ?? ''
      const match = disposition.match(/filename="?([^";\n]+)"?/)
      const filename = match?.[1] ?? `audr-export-${kind}.${format}`

      const reader = response.body?.getReader()
      const chunks: Uint8Array<ArrayBuffer>[] = []
      let bytesReceived = 0

      if (reader) {
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          chunks.push(value)
          bytesReceived += value.length
          setProgress(`Downloading… ${(bytesReceived / 1024).toFixed(1)} KB`)
        }
      }

      const blob = new Blob(chunks, {
        type: format === 'json' ? 'application/json' : 'text/csv',
      })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)

      setExportState('done')
      setProgress(`Download complete — ${(bytesReceived / 1024).toFixed(1)} KB`)
    } catch (err) {
      if ((err as Error).name === 'AbortError') {
        setExportState('idle')
        setProgress(null)
      } else {
        setExportState('error')
        setExportError(errorMessage(err, 'Export failed.'))
      }
    } finally {
      abortRef.current = null
    }
  }

  function handleAbort() {
    abortRef.current?.abort()
  }

  return (
    <section aria-label="Export data" className="card">
      <h2 className="section-heading mb-8">Export Data</h2>
      <p className="muted-text mb-16">
        Download a complete export of your portfolio data. Exports include wallet addresses, asset metadata,
        balances, quotes, and valuation history. They do not include provider credentials or the owner
        password.
      </p>

      <form
        onSubmit={(e) => {
          e.preventDefault()
          void handleExport()
        }}
        aria-label="Export options"
        className="form-grid"
      >
        <div>
          <div className="form-label mb-8">Export content</div>
          <div style={{ display: 'grid', gap: 8 }}>
            <label className="toggle-row">
              <input
                type="radio"
                name="export-kind"
                value="portfolio"
                checked={kind === 'portfolio'}
                onChange={() => setKind('portfolio')}
                disabled={exportState === 'streaming'}
                className="checkbox-folio"
              />
              <span className="toggle-label">Current portfolio (wallets, assets, holdings, valuation)</span>
            </label>
            <label className="toggle-row">
              <input
                type="radio"
                name="export-kind"
                value="history"
                checked={kind === 'history'}
                onChange={() => setKind('history')}
                disabled={exportState === 'streaming'}
                className="checkbox-folio"
              />
              <span className="toggle-label">Full history (all recorded observations and valuations)</span>
            </label>
          </div>
        </div>

        <div>
          <div className="form-label mb-8">Format</div>
          <div className="row gap-12">
            <label className="toggle-row">
              <input
                type="radio"
                name="export-format"
                value="json"
                checked={format === 'json'}
                onChange={() => setFormat('json')}
                disabled={exportState === 'streaming'}
                className="checkbox-folio"
              />
              <span className="toggle-label">JSON</span>
            </label>
            <label className="toggle-row">
              <input
                type="radio"
                name="export-format"
                value="csv"
                checked={format === 'csv'}
                onChange={() => setFormat('csv')}
                disabled={exportState === 'streaming'}
                className="checkbox-folio"
              />
              <span className="toggle-label">CSV</span>
            </label>
          </div>
        </div>

        {exportState !== 'streaming' ? (
          <button
            type="submit"
            className="btn btn-primary"
            style={{ width: 'fit-content' }}
            aria-label={`Export ${kind} as ${format.toUpperCase()}`}
          >
            Export {kind} as {format.toUpperCase()}
          </button>
        ) : (
          <button type="button" className="btn btn-ghost" onClick={handleAbort} aria-label="Cancel export">
            Cancel export
          </button>
        )}
      </form>

      {progress !== null && (
        <p role="status" aria-live="polite" className="muted-text mt-12">
          {progress}
        </p>
      )}
      {exportError !== null && (
        <p role="alert" className="alert alert-danger mt-12">
          {exportError}
        </p>
      )}
    </section>
  )
}
