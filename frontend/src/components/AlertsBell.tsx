import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { IconBell } from './Icons'
import { fetchAlerts, markAlertRead, ApiError } from '../api/client'

const POLL_INTERVAL_MS = 60_000

const SEVERITY_BADGE: Record<string, string> = {
  critical: 'badge-danger',
  warning: 'badge-warning',
}

function formatCreatedAt(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}

export default function AlertsBell() {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  const queryClient = useQueryClient()

  const { data, error } = useQuery({
    queryKey: ['alerts'],
    queryFn: () => fetchAlerts(),
    refetchInterval: POLL_INTERVAL_MS,
  })

  useEffect(() => {
    if (!open) return

    function handlePointerDown(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handlePointerDown)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('mousedown', handlePointerDown)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [open])

  const unreadCount = data?.unread_count ?? 0
  const alerts = data?.alerts ?? []

  async function handleMarkRead(alertId: string) {
    try {
      await markAlertRead(alertId)
      await queryClient.invalidateQueries({ queryKey: ['alerts'] })
    } catch {
      // Best-effort — a failed mark-read leaves the alert unread, which is
      // the safe side to fail on; the next poll will show accurate state.
    }
  }

  return (
    <div className="alerts-bell" ref={rootRef}>
      <button
        type="button"
        className="theme-toggle alerts-bell-button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={unreadCount > 0 ? `${unreadCount} unread alerts` : 'Alerts'}
      >
        <IconBell width={17} height={17} />
        {unreadCount > 0 && (
          <span className="badge badge-danger alerts-bell-badge" aria-hidden="true">
            {unreadCount > 9 ? '9+' : unreadCount}
          </span>
        )}
      </button>
      {open && (
        <div className="user-menu-dropdown alerts-bell-dropdown" role="menu">
          <div className="alerts-bell-header">Alerts</div>
          {error && (
            <p role="alert" className="alert alert-danger">
              {error instanceof ApiError ? error.message : 'Failed to load alerts.'}
            </p>
          )}
          {!error && alerts.length === 0 && (
            <p role="note" className="text-muted alerts-bell-empty">
              No alerts yet.
            </p>
          )}
          {alerts.length > 0 && (
            <ul role="list" className="alerts-bell-list">
              {alerts.map((a) => (
                <li
                  key={a.id}
                  className={`alerts-bell-item${a.read_at ? '' : ' alerts-bell-item-unread'}`}
                >
                  <button
                    type="button"
                    className="alerts-bell-item-button"
                    onClick={() => void handleMarkRead(a.id)}
                    disabled={a.read_at !== null}
                    aria-label={a.read_at ? 'Already read' : 'Mark as read'}
                  >
                    <span
                      className={`badge ${SEVERITY_BADGE[a.severity] ?? 'badge-neutral'} alerts-bell-item-severity`}
                    >
                      {a.category.replace('_', ' ')}
                    </span>
                    <span className="alerts-bell-item-summary">{a.summary}</span>
                    <span className="alerts-bell-item-meta text-secondary">
                      {formatCreatedAt(a.created_at)}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
