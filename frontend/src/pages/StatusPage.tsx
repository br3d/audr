import { useState } from 'react'
import { useQuery, useInfiniteQuery, useQueryClient } from '@tanstack/react-query'
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

function jobStatusBadge(status: JobRun['status']) {
  switch (status) {
    case 'pending':
      return <span className="badge badge-neutral">Pending</span>
    case 'running':
      return <span className="badge badge-info">Running</span>
    case 'completed':
      return <span className="badge badge-ok">Completed</span>
    case 'failed':
      return <span className="badge badge-error">Failed</span>
    case 'cancelled':
      return <span className="badge badge-neutral">Cancelled</span>
    default:
      return <span className="badge badge-neutral">{status}</span>
  }
}

function dbStatusDot(status: string) {
  if (status === 'ok') return 'status-dot-ok'
  if (status === 'degraded') return 'status-dot-warn'
  return 'status-dot-err'
}

function workerStatusDot(status: string) {
  if (status === 'running') return 'status-dot-ok'
  if (status === 'stopped') return 'status-dot-err'
  return 'status-dot-muted'
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
      setCancelError(err instanceof ApiError ? err.message : 'Failed to cancel job.')
    } finally {
      setCancelling(false)
    }
  }

  const isActive = job.status === 'pending' || job.status === 'running'

  return (
    <tr>
      <td className="fw-500">{job.kind}</td>
      <td>
        <span aria-label={`Job status: ${job.status}`} data-status={job.status}>
          {jobStatusBadge(job.status)}
        </span>
      </td>
      <td className="td-muted">{formatTimestamp(job.started_at)}</td>
      <td className="td-muted">{formatTimestamp(job.finished_at)}</td>
      <td className="td-muted">
        {job.attempted}/{job.succeeded}/{job.failed}
      </td>
      <td>
        {job.error_message && (
          <span className="text-danger" title={job.error_message}>
            {job.error_message.slice(0, 40)}
            {job.error_message.length > 40 ? '…' : ''}
          </span>
        )}
        {isActive && (
          <div>
            <button
              type="button"
              className="btn btn-sm btn-danger"
              onClick={handleCancel}
              disabled={cancelling}
              aria-label={`Cancel ${job.kind} job`}
            >
              {cancelling ? 'Cancelling…' : 'Cancel'}
            </button>
            {cancelError !== null && (
              <p role="alert" className="alert alert-danger mt-4">
                {cancelError}
              </p>
            )}
          </div>
        )}
      </td>
    </tr>
  )
}

function SystemStatus({ status }: { status: StatusResponse }) {
  const dbStatus = status.db?.status ?? 'unknown'
  const workerStatus = status.worker?.status ?? 'unknown'

  return (
    <section aria-label="System status">
      <div className="status-grid">
        <div className="status-item">
          <div className="status-item-label">Database</div>
          <div className="status-item-value row gap-8">
            <span className={`status-dot ${dbStatusDot(dbStatus)}`} />
            {dbStatus}
          </div>
        </div>

        <div className="status-item">
          <div className="status-item-label">Worker</div>
          <div className="status-item-value row gap-8">
            <span className={`status-dot ${workerStatusDot(workerStatus)}`} />
            {workerStatus}
          </div>
        </div>

        <div className="status-item">
          <div className="status-item-label">Last Heartbeat</div>
          <div className="status-item-value">
            {formatTimestamp(status.worker?.last_heartbeat_at ?? null)}
          </div>
        </div>

        {status.recovery && (
          <div className="status-item">
            <div className="status-item-label">Recovery mode</div>
            <div className="status-item-value row gap-8">
              {status.recovery.active ? (
                <>
                  <span className="status-dot status-dot-warn" />
                  Active
                </>
              ) : (
                <>
                  <span className="status-dot status-dot-ok" />
                  None
                </>
              )}
            </div>
          </div>
        )}

        {status.version && (
          <div className="status-item">
            <div className="status-item-label">Version</div>
            <div className="status-item-value td-mono">{status.version}</div>
          </div>
        )}
      </div>

      {status.schedules && (
        <div className="card">
          <div className="section-heading mb-12">Next scheduled runs</div>
          <div style={{ display: 'grid', gap: 8 }}>
            <div className="row-between">
              <span className="text-secondary">Balance scan</span>
              <span className="td-muted">
                {formatTimestamp(status.schedules.balances?.next_due_at ?? null)}
              </span>
            </div>
            <div className="row-between">
              <span className="text-secondary">Discovery</span>
              <span className="td-muted">
                {formatTimestamp(status.schedules.discovery?.next_due_at ?? null)}
              </span>
            </div>
            <div className="row-between">
              <span className="text-secondary">Quotes</span>
              <span className="td-muted">
                {formatTimestamp(status.schedules.quotes?.next_due_at ?? null)}
              </span>
            </div>
          </div>
        </div>
      )}
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

  const jobsQuery = useInfiniteQuery({
    queryKey: ['jobs'],
    queryFn: ({ pageParam }) => fetchJobs(undefined, pageParam as string | undefined),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
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
    <div>
      {statusQuery.isLoading && <p aria-busy="true">Loading system status…</p>}
      {statusQuery.error && (
        <p role="alert" className="alert alert-danger mb-16">
          Failed to load system status.{' '}
          {statusQuery.error instanceof ApiError
            ? statusQuery.error.message
            : 'Please try again.'}
        </p>
      )}
      {statusQuery.data && <SystemStatus status={statusQuery.data} />}

      <section aria-label="Manual triggers" className="card mt-20">
        <div className="section-heading mb-8">Manual triggers</div>
        <p className="muted-text mb-12">Queue a job immediately, bypassing the schedule.</p>
        <div className="btn-group" role="group" aria-label="Trigger jobs">
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => handleTrigger('balances')}
            aria-label="Trigger balance scan"
          >
            Trigger balance scan
          </button>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => handleTrigger('discovery')}
            aria-label="Trigger token discovery"
          >
            Trigger discovery
          </button>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => handleTrigger('quotes')}
            aria-label="Trigger quote fetch"
          >
            Trigger quote fetch
          </button>
        </div>
        {triggerMsg !== null && (
          <p role="status" className="alert alert-success mt-12">
            {triggerMsg}
          </p>
        )}
        {triggerError !== null && (
          <p role="alert" className="alert alert-danger mt-12">
            {triggerError}
          </p>
        )}
      </section>

      <section aria-label="Job history" className="mt-20">
        <div className="section-heading mb-12">Job history</div>
        {jobsQuery.isLoading && <p aria-busy="true">Loading job history…</p>}
        {jobsQuery.error && (
          <p role="alert" className="alert alert-danger">
            Failed to load jobs.{' '}
            {jobsQuery.error instanceof ApiError
              ? jobsQuery.error.message
              : 'Please try again.'}
          </p>
        )}
        {(() => {
          const jobs = jobsQuery.data?.pages.flatMap((p) => p.items) ?? []
          if (jobsQuery.data && jobs.length === 0) {
            return (
              <div className="empty-state">
                <div className="empty-state-text">No jobs have run yet</div>
              </div>
            )
          }
          if (jobs.length > 0) {
            return (
              <>
                <div className="table-container">
                  <table className="table-folio" aria-label="Job runs">
                    <thead>
                      <tr>
                        <th scope="col">Kind</th>
                        <th scope="col">Status</th>
                        <th scope="col">Started</th>
                        <th scope="col">Finished</th>
                        <th scope="col">A/S/F</th>
                        <th scope="col">Details</th>
                      </tr>
                    </thead>
                    <tbody>
                      {jobs.map((job) => (
                        <JobRow key={job.id} job={job} onCancelled={invalidateAll} />
                      ))}
                    </tbody>
                  </table>
                </div>
                {jobsQuery.hasNextPage && (
                  <div className="mt-16" style={{ textAlign: 'center' }}>
                    <button
                      type="button"
                      className="btn btn-secondary"
                      onClick={() => void jobsQuery.fetchNextPage()}
                      disabled={jobsQuery.isFetchingNextPage}
                    >
                      {jobsQuery.isFetchingNextPage ? 'Loading…' : 'Load more'}
                    </button>
                  </div>
                )}
              </>
            )
          }
          return null
        })()}
      </section>
    </div>
  )
}
