import { useState } from 'react'
import type { FormEvent } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchSettings, patchSettings, ApiError } from '../api/client'
import type { SettingsResponse } from '../api/client'

function costWarning(
  intervalSeconds: number,
  kind: 'balances' | 'discovery' | 'quotes',
): string | null {
  const perDay = Math.floor(86400 / intervalSeconds)
  if (kind === 'quotes' && intervalSeconds < 300) {
    return `Fetching quotes every ${intervalSeconds}s runs ~${perDay} requests/day. Free-tier providers cap at 50–100 req/day. Consider 300s or longer.`
  }
  if (kind === 'balances' && intervalSeconds < 60) {
    return `Scanning balances every ${intervalSeconds}s makes ~${perDay} RPC calls/day. This may exhaust free RPC quotas quickly.`
  }
  if (kind === 'discovery' && intervalSeconds < 3600) {
    return `Discovery every ${intervalSeconds}s is aggressive. 1h or longer is recommended.`
  }
  return null
}

function freshnessLabel(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`
  return `${Math.round(seconds / 86400)}d`
}

interface ScheduleCardProps {
  id: string
  label: string
  kind: 'balances' | 'discovery' | 'quotes'
  intervalSeconds: number
  enabled: boolean
  nextDueAt?: string | null
  onChange: (intervalSeconds: number, enabled: boolean) => void
  disabled: boolean
}

function ScheduleCard({
  id,
  label,
  kind,
  intervalSeconds,
  enabled,
  nextDueAt,
  onChange,
  disabled,
}: ScheduleCardProps) {
  const warning = costWarning(intervalSeconds, kind)

  return (
    <div className="schedule-card">
      <div className="schedule-card-header">
        <span className="schedule-name">{label}</span>
        <label className="toggle-row" style={{ cursor: 'pointer' }}>
          <input
            id={`${id}-enabled`}
            type="checkbox"
            className="checkbox-folio"
            checked={enabled}
            disabled={disabled}
            onChange={(e) => onChange(intervalSeconds, e.target.checked)}
            aria-label={`Enable ${label}`}
          />
          <span className="toggle-label">{enabled ? 'Enabled' : 'Disabled'}</span>
        </label>
      </div>

      {enabled && (
        <div className="schedule-fields">
          <div className="form-group">
            <label htmlFor={`${id}-interval`} className="form-label">
              Interval — currently <strong>{freshnessLabel(intervalSeconds)}</strong>
            </label>
            <input
              id={`${id}-interval`}
              type="number"
              className="input-folio"
              min={30}
              max={86400}
              step={30}
              value={intervalSeconds}
              disabled={disabled}
              style={{ maxWidth: 160 }}
              onChange={(e) => {
                const v = parseInt(e.target.value, 10)
                if (!isNaN(v) && v >= 30) onChange(v, enabled)
              }}
              aria-describedby={warning ? `${id}-warning` : undefined}
            />
            {warning && (
              <p id={`${id}-warning`} role="note" className="alert alert-warning mt-8">
                {warning}
              </p>
            )}
          </div>
        </div>
      )}

      {nextDueAt && (
        <p className="muted-text mt-8">
          Next run:{' '}
          {new Date(nextDueAt).toLocaleString('en-US', {
            month: 'short',
            day: 'numeric',
            hour: '2-digit',
            minute: '2-digit',
            hour12: false,
          })}
        </p>
      )}
    </div>
  )
}

function usageProjection(settings: SettingsResponse): string {
  const balance = settings.schedules?.balances
  const discovery = settings.schedules?.discovery
  const quotes = settings.schedules?.quotes

  const parts: string[] = []
  if (balance?.enabled && balance.interval_seconds) {
    const perDay = Math.round(86400 / balance.interval_seconds)
    parts.push(`~${perDay} balance scans/day`)
  }
  if (discovery?.enabled && discovery.interval_seconds) {
    const perDay = Math.round(86400 / discovery.interval_seconds)
    parts.push(`~${perDay} discovery runs/day`)
  }
  if (quotes?.enabled && quotes.interval_seconds) {
    const perDay = Math.round(86400 / quotes.interval_seconds)
    parts.push(`~${perDay} quote fetches/day`)
  }
  return parts.length ? parts.join(' · ') : 'No schedules enabled.'
}

export default function SchedulesPage() {
  const queryClient = useQueryClient()

  const { data, error, isLoading } = useQuery({
    queryKey: ['settings'],
    queryFn: fetchSettings,
  })

  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [saveMsg, setSaveMsg] = useState<string | null>(null)
  const [draft, setDraft] = useState<SettingsResponse | null>(null)

  const current = draft ?? data ?? null

  function updateSchedule(
    kind: 'balances' | 'discovery' | 'quotes',
    intervalSeconds: number,
    enabled: boolean,
  ) {
    if (!current) return
    setDraft({
      ...current,
      schedules: {
        ...current.schedules,
        [kind]: { ...(current.schedules?.[kind] ?? {}), interval_seconds: intervalSeconds, enabled },
      },
    })
    setSaveMsg(null)
    setSaveError(null)
  }

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    if (!current || !data) return
    setSaveError(null)
    setSaveMsg(null)
    setSaving(true)
    try {
      await patchSettings({
        revision: data.revision,
        schedules: current.schedules,
      })
      setSaveMsg('Schedule settings saved.')
      setDraft(null)
      void queryClient.invalidateQueries({ queryKey: ['settings'] })
    } catch (err) {
      setSaveError(err instanceof ApiError ? err.message : 'Failed to save settings.')
    } finally {
      setSaving(false)
    }
  }

  if (isLoading) return <p aria-busy="true">Loading schedule settings…</p>

  if (error) {
    return (
      <p role="alert" className="alert alert-danger">
        Failed to load settings.{' '}
        {error instanceof ApiError ? error.message : 'Please try again.'}
      </p>
    )
  }

  if (!current) return null

  const balances = current.schedules?.balances
  const discovery = current.schedules?.discovery
  const quotes = current.schedules?.quotes

  return (
    <div>
      <p className="page-subheading">
        Configure how often audr fetches balances, discovers tokens, and retrieves price
        quotes. Changes take effect after the current run completes.
      </p>

      <div className="card mb-20">
        <div className="metric-label mb-8">Projected usage</div>
        <p className="fw-600">{usageProjection(current)}</p>
        <p className="muted-text mt-8">
          Free-tier providers typically cap at 100–10,000 requests/day. Set intervals
          conservatively and monitor your provider dashboard.
        </p>
      </div>

      <form onSubmit={handleSubmit} aria-label="Schedule settings">
        <div style={{ display: 'grid', gap: 14, marginBottom: 20 }}>
          <ScheduleCard
            id="balances"
            label="Balance scans"
            kind="balances"
            intervalSeconds={balances?.interval_seconds ?? 300}
            enabled={balances?.enabled ?? true}
            nextDueAt={balances?.next_due_at}
            onChange={(s, en) => updateSchedule('balances', s, en)}
            disabled={saving}
          />

          <ScheduleCard
            id="discovery"
            label="Token discovery"
            kind="discovery"
            intervalSeconds={discovery?.interval_seconds ?? 3600}
            enabled={discovery?.enabled ?? true}
            nextDueAt={discovery?.next_due_at}
            onChange={(s, en) => updateSchedule('discovery', s, en)}
            disabled={saving}
          />

          <ScheduleCard
            id="quotes"
            label="Price quotes"
            kind="quotes"
            intervalSeconds={quotes?.interval_seconds ?? 300}
            enabled={quotes?.enabled ?? true}
            nextDueAt={quotes?.next_due_at}
            onChange={(s, en) => updateSchedule('quotes', s, en)}
            disabled={saving}
          />
        </div>

        {saveError !== null && (
          <p role="alert" className="alert alert-danger mb-12">
            {saveError}
          </p>
        )}
        {saveMsg !== null && (
          <p role="status" className="alert alert-success mb-12">
            {saveMsg}
          </p>
        )}

        <div className="btn-group">
          <button
            type="submit"
            className="btn btn-primary"
            disabled={saving || draft === null}
          >
            {saving ? 'Saving…' : 'Save schedule settings'}
          </button>
          {draft !== null && (
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => {
                setDraft(null)
                setSaveError(null)
                setSaveMsg(null)
              }}
              disabled={saving}
            >
              Discard changes
            </button>
          )}
        </div>
      </form>
    </div>
  )
}
