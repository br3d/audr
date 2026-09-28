import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchStatus, fetchJobs, cancelJob, triggerJob, ApiError } from '../api/client'
import type { StatusResponse, JobRun } from '../api/client'

function formatTimestamp(ts: string | null): string {
  if (!ts) return '—'
  return new Date(ts).toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

function jobStatusLabel(status: JobRun['status']): string {
  switch (status) {
    case 'pending': return 'Pending'
    case 'running': return 'Running'
    case 'completed': return 'Completed'
    case 'failed': return 'Failed'
    case 'cancelled': return 'Cancelled'
    default: return status
  }
}

function JobRow({ job, onCancelled }: { job: JobRun; onCancelled: () => void }) {
  const [cancelling, setCancelling] = useState(false)
  const [cancelError, setCancelError] = useState<string | null>(null)

  async function handleCancel() {
    setCancelError(null)
    setCancelling(true)
    try {
      await cancelJob(job.id)
      onCancelled()
    } catch (err) {
      setCancelError(
        err instanceof ApiError ? err.message : 'Failed to cancel job.',
      )
    } finally {
      setCancelling(false)
    }
  }

  const isActive = job.status === 'pending' || job.status === 'running'

  return (
    <li>
      <dl>
        <dt>Kind</dt>
        <dd>{job.kind}</dd>
        <dt>Status</dt>
        <dd
          aria-label={`Job status: ${jobStatusLabel(job.status)}`}
          data-status={job.status}
        >
          {jobStatusLabel(job.status)}
        </dd>
        <dt>Last attempt</dt>
        <dd>{formatTimestamp(job.started_at)}</dd>
        <dt>Last success</dt>
        <dd>{job.status === 'completed' ? formatTimestamp(job.finished_at) : '—'}</dd>
        <dt>Finished</dt>
        <dd>{formatTimestamp(job.finished_at)}</dd>
        <dt>Attempted / Succeeded / Failed</dt>
        <dd>
          {job.attempted} / {job.succeeded} / {job.failed}
        </dd>
        {job.error_message && (
          <>
            <dt>Error</dt>
            <dd>{job.error_message}</dd>
          </>
        )}
      </dl>
      {isActive && (
        <div>
          <button
            type="button"
            onClick={handleCancel}
            disabled={cancelling}
            aria-label={`Cancel ${job.kind} job`}
          >
            {cancelling ? 'Cancelling…' : 'Cancel job'}
          </button>
          {cancelError !== null && <p role="alert">{cancelError}</p>}
        </div>
      )}
    </li>
  )
}

function SystemStatus({ status }: { status: StatusResponse }) {
  return (
    <section aria-label="System status">
      <h2>System status</h2>
      <dl>
        <dt>Database</dt>
        <dd>{status.db?.status ?? 'unknown'}</dd>
        <dt>Worker heartbeat</dt>
        <dd>{status.worker?.last_heartbeat_at ? formatTimestamp(status.worker.last_heartbeat_at) : 'No heartbeat received'}</dd>
        <dt>Worker status</dt>
        <dd>{status.worker?.status ?? 'unknown'}</dd>
        {status.recovery && (
          <>
            <dt>Recovery mode</dt>
            <dd>{status.recovery.active ? 'Active — system is recovering' : 'None'}</dd>
          </>
        )}
        {status.schedules && (
          <>
            <dt>Balance scan — next execution</dt>
            <dd>{formatTimestamp(status.schedules.balances?.next_due_at ?? null)}</dd>
            <dt>Discovery — next execution</dt>
            <dd>{formatTimestamp(status.schedules.discovery?.next_due_at ?? null)}</dd>
            <dt>Quotes — next execution</dt>
            <dd>{formatTimestamp(status.schedules.quotes?.next_due_at ?? null)}</dd>
          </>
        )}
        <dt>Version</dt>
        <dd>{status.version ?? 'unknown'}</dd>
      </dl>
    </section>
  )
}

export default function StatusPage() {
  const queryClient = useQueryClient()
  const [triggerMsg, setTriggerMsg] = useState<string | null>(null)
  const [triggerError, setTriggerError] = useState<string | null>(null)

  const statusQuery = useQuery({
    queryKey: ['system-status'],
    queryFn: fetchStatus,
    refetchInterval: 15000,
  })

  const jobsQuery = useQuery({
    queryKey: ['jobs'],
    queryFn: () => fetchJobs(),
    refetchInterval: 10000,
  })

  function invalidateAll() {
    void queryClient.invalidateQueries({ queryKey: ['jobs'] })
    void queryClient.invalidateQueries({ queryKey: ['system-status'] })
  }

  async function handleTrigger(kind: 'balances' | 'discovery' | 'quotes') {
    setTriggerMsg(null)
    setTriggerError(null)
    try {
      await triggerJob(kind)
      setTriggerMsg(`${kind} job queued.`)
      invalidateAll()
    } catch (err) {
      setTriggerError(err instanceof ApiError ? err.message : 'Failed to queue job.')
    }
  }

  return (
    <main>
      <h1>Status</h1>
      <p>Current worker and job status. Job history refreshes every 10 seconds.</p>

      {statusQuery.isLoading && <p>Loading system status…</p>}
      {statusQuery.error && (
        <p role="alert">
          Failed to load system status.{' '}
          {statusQuery.error instanceof ApiError
            ? statusQuery.error.message
            : 'Please try again.'}
        </p>
      )}
      {statusQuery.data && <SystemStatus status={statusQuery.data} />}

      <section aria-label="Manual triggers">
        <h2>Manual triggers</h2>
        <p>Queue a job immediately, bypassing the schedule.</p>
        <div role="group" aria-label="Trigger jobs">
          <button
            type="button"
            onClick={() => handleTrigger('balances')}
            aria-label="Trigger balance scan"
          >
            Trigger balance scan
          </button>
          <button
            type="button"
            onClick={() => handleTrigger('discovery')}
            aria-label="Trigger token discovery"
          >
            Trigger discovery
          </button>
          <button
            type="button"
            onClick={() => handleTrigger('quotes')}
            aria-label="Trigger quote fetch"
          >
            Trigger quote fetch
          </button>
        </div>
        {triggerMsg !== null && <p role="status">{triggerMsg}</p>}
        {triggerError !== null && <p role="alert">{triggerError}</p>}
      </section>

      <section aria-label="Job history">
        <h2>Job history</h2>
        {jobsQuery.isLoading && <p>Loading job history…</p>}
        {jobsQuery.error && (
          <p role="alert">
            Failed to load jobs.{' '}
            {jobsQuery.error instanceof ApiError
              ? jobsQuery.error.message
              : 'Please try again.'}
          </p>
        )}
        {jobsQuery.data && jobsQuery.data.items.length === 0 && (
          <p>No jobs have run yet.</p>
        )}
        {jobsQuery.data && jobsQuery.data.items.length > 0 && (
          <ul aria-label="Job runs">
            {jobsQuery.data.items.map((job) => (
              <JobRow key={job.id} job={job} onCancelled={invalidateAll} />
            ))}
          </ul>
        )}
      </section>
    </main>
  )
}
