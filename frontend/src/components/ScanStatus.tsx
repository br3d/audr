import { useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchJob, triggerJob, ApiError } from '../api/client'
import type { JobRun, JobKind } from '../api/client'
import { useEffect, useRef, useState } from 'react'
import { IconRefresh, IconSearch } from './Icons'

// Terminal job states — once a run reaches one of these, polling stops and
// whatever the run produced is ready to be read back.
const TERMINAL: ReadonlySet<string> = new Set(['completed', 'failed', 'cancelled'])

interface Props {
  runId: string | null
  kind: JobKind
  label: string
}

export default function ScanStatus({ runId, kind, label }: Props) {
  const [triggeredId, setTriggeredId] = useState<string | null>(null)
  const [triggerError, setTriggerError] = useState<string | null>(null)
  const [triggering, setTriggering] = useState(false)

  const activeRunId = triggeredId ?? runId

  const { data: job } = useQuery({
    queryKey: ['job', activeRunId],
    queryFn: () => fetchJob(activeRunId!),
    enabled: activeRunId !== null,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      if (status === 'running' || status === 'pending') return 3000
      return false
    },
  })

  // AUD-446: a finished scan wrote new balances and republished the portfolio
  // snapshot, but nothing told react-query about it — so the allocation table
  // kept rendering its cached pre-scan data and the whole refresh looked like
  // it had done nothing until the user reloaded the page by hand. Refetch the
  // views a scan can change as soon as the run reaches a terminal state.
  const queryClient = useQueryClient()
  const lastSettledRunId = useRef<string | null>(null)
  useEffect(() => {
    if (activeRunId === null || job === undefined) return
    if (!TERMINAL.has(job.status)) return
    // Invalidate once per run, not on every poll tick after it settles.
    if (lastSettledRunId.current === activeRunId) return
    lastSettledRunId.current = activeRunId
    void queryClient.invalidateQueries({ queryKey: ['portfolio'] })
    void queryClient.invalidateQueries({ queryKey: ['assets'] })
    void queryClient.invalidateQueries({ queryKey: ['history'] })
  }, [activeRunId, job, queryClient])

  async function handleTrigger() {
    setTriggerError(null)
    setTriggering(true)
    try {
      const ref = await triggerJob(kind)
      setTriggeredId(ref.run_id)
    } catch (err) {
      setTriggerError(
        err instanceof ApiError ? err.message : `Failed to start ${label}.`,
      )
    } finally {
      setTriggering(false)
    }
  }

  function statusLabel(j: JobRun): string {
    switch (j.status) {
      case 'pending':
        return `${label}: queued`
      // AUD-446: the old "(x / y processed)" counter was not a progress
      // readout. The API synthesises attempted/succeeded/failed from
      // retry_count alone, so a running job always rendered "0 / 1 processed"
      // for its whole multi-minute duration and read as a frozen UI. Report
      // the state we actually know instead of a counter that never moves.
      case 'running':
        return `${label}: running…`
      case 'completed':
        return j.failed > 0
          ? `${label}: complete (after ${j.failed} failed ${j.failed === 1 ? 'attempt' : 'attempts'})`
          : `${label}: complete`
      case 'failed':
        return `${label}: failed${j.error_message ? ` — ${j.error_message}` : ''}`
      case 'cancelled':
        return `${label}: cancelled`
    }
  }

  // AUD-415: map job state onto the shared status-dot / text tone classes so the
  // scan controls read like the rest of the Folio UI instead of raw browser text.
  function statusTone(j: JobRun): { dot: string; text: string } {
    switch (j.status) {
      case 'completed':
        return j.failed > 0
          ? { dot: 'status-dot-warn', text: 'scan-status-msg-warn' }
          : { dot: 'status-dot-ok', text: 'scan-status-msg-ok' }
      case 'failed':
        return { dot: 'status-dot-err', text: 'scan-status-msg-error' }
      case 'cancelled':
        return { dot: 'status-dot-muted', text: '' }
      default:
        return { dot: 'status-dot-warn', text: '' }
    }
  }

  const busy = triggering || job?.status === 'running' || job?.status === 'pending'
  const Icon = kind === 'discovery' ? IconSearch : IconRefresh

  return (
    <div className="scan-status" aria-label={`${label} status`}>
      <button
        type="button"
        className="btn btn-sm btn-secondary"
        onClick={() => void handleTrigger()}
        disabled={busy}
        aria-label={`Start ${label}`}
        title={`${label} for every tracked address — spends an RPC call per address.`}
      >
        <Icon width={14} height={14} />
        {triggering ? 'Starting…' : label}
      </button>
      {job !== undefined && (
        <p
          role="status"
          aria-live="polite"
          className={`scan-status-msg ${statusTone(job).text}`}
        >
          <span className={`status-dot ${statusTone(job).dot}`} aria-hidden="true" />
          {statusLabel(job)}
        </p>
      )}
      {triggerError !== null && (
        <p role="alert" className="scan-status-msg scan-status-msg-error">
          <span className="status-dot status-dot-err" aria-hidden="true" />
          {triggerError}
        </p>
      )}
    </div>
  )
}
