import { useQuery } from '@tanstack/react-query'
import { fetchJob, triggerJob, ApiError } from '../api/client'
import type { JobRun, JobKind } from '../api/client'
import { useState } from 'react'

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
      case 'running':
        return `${label}: running (${j.succeeded + j.failed} / ${j.attempted} processed)`
      case 'completed':
        return `${label}: complete — ${j.succeeded} succeeded, ${j.failed} failed`
      case 'failed':
        return `${label}: failed${j.error_message ? ` — ${j.error_message}` : ''}`
      case 'cancelled':
        return `${label}: cancelled`
    }
  }

  return (
    <div aria-label={`${label} status`}>
      {job !== undefined && (
        <p role="status" aria-live="polite">
          {statusLabel(job)}
        </p>
      )}
      {triggerError !== null && <p role="alert">{triggerError}</p>}
      <button
        type="button"
        onClick={handleTrigger}
        disabled={triggering || job?.status === 'running' || job?.status === 'pending'}
        aria-label={`Start ${label}`}
      >
        {triggering ? 'Starting…' : label}
      </button>
    </div>
  )
}
