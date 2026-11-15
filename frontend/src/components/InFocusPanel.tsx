import { useMemo } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchAlerts, fetchAllowances, fetchAssets, markAlertRead } from '../api/client'
import type { AllocationItem } from '../api/client'
import { queryKeys } from '../api/queryKeys'
import type { MainPage } from './Layout'
import {
  IN_FOCUS_MAX_ITEMS,
  allowanceSignals,
  categoryLabel,
  marketMoveSignals,
  orderSignals,
} from '../portfolio/inFocus'
import type { InFocusSignal } from '../portfolio/inFocus'

const POLL_MS = 60_000

interface Props {
  allocations: AllocationItem[]
  setPage: (page: MainPage) => void
}

/**
 * "In focus" (AUD-947): real signals only — unread news alerts, big 24h
 * moves, unlimited token allowances. No LLM, no placeholder items. Each
 * source degrades on its own: a failed query drops only its own signals.
 */
export default function InFocusPanel({ allocations, setPage }: Props) {
  const queryClient = useQueryClient()

  const alertsQuery = useQuery({
    queryKey: queryKeys.alertsUnread(),
    queryFn: () => fetchAlerts(true),
    refetchInterval: POLL_MS,
  })
  const allowancesQuery = useQuery({
    queryKey: queryKeys.allowancesUnlimited(),
    queryFn: () => fetchAllowances({ unlimitedOnly: true }),
    refetchInterval: POLL_MS,
  })
  // Allowances carry only a token address; held assets give us the ticker.
  // Failing this lookup just means the short address is shown instead.
  const assetsQuery = useQuery({
    queryKey: queryKeys.assetsSymbolLookup(),
    queryFn: () => fetchAssets({ held: true }),
    enabled: (allowancesQuery.data?.allowances.length ?? 0) > 0,
  })

  const markRead = useMutation({
    mutationFn: (id: string) => markAlertRead(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.alerts() }),
  })

  const signals = useMemo(() => {
    const symbols: Record<string, string> = {}
    for (const a of assetsQuery.data?.items ?? []) {
      if (a.contract_address) symbols[a.contract_address.toLowerCase()] = a.symbol
    }
    return orderSignals(
      alertsQuery.data?.alerts ?? [],
      marketMoveSignals(allocations),
      allowanceSignals(allowancesQuery.data?.allowances ?? [], symbols),
    )
  }, [alertsQuery.data, allowancesQuery.data, assetsQuery.data, allocations])

  const shown = signals.slice(0, IN_FOCUS_MAX_ITEMS)
  const hidden = signals.length - shown.length
  const failed = [alertsQuery.isError && 'alerts', allowancesQuery.isError && 'allowances'].filter(Boolean)

  return (
    <section aria-label="In focus" className="card infocus mb-20">
      <div className="card-header">
        <div className="card-title">In focus</div>
        {signals.length > 0 && (
          <span className="pill risk">{signals.length === 1 ? '1 signal' : `${signals.length} signals`}</span>
        )}
      </div>

      {signals.length === 0 ? (
        <p role="note" className="muted-text">
          Nothing needs your attention right now.
        </p>
      ) : (
        <ul role="list" className="infocus-list">
          {shown.map((s) => (
            <SignalRow
              key={s.key}
              signal={s}
              setPage={setPage}
              onMarkRead={(id) => markRead.mutate(id)}
              marking={markRead.isPending}
            />
          ))}
        </ul>
      )}

      {hidden > 0 && (
        <button type="button" className="link-button infocus-more" onClick={() => setPage('events')}>
          +{hidden} more
        </button>
      )}

      {failed.length > 0 && (
        <p role="note" className="text-muted infocus-partial">
          Couldn’t check {failed.join(' or ')} just now.
        </p>
      )}
    </section>
  )
}

function SignalRow({
  signal,
  setPage,
  onMarkRead,
  marking,
}: {
  signal: InFocusSignal
  setPage: (page: MainPage) => void
  onMarkRead: (id: string) => void
  marking: boolean
}) {
  if (signal.kind === 'alert') {
    const a = signal.alert
    return (
      <li className="infocus-item">
        <div className="infocus-head">
          <span className={`pill ${signal.severity === 'critical' ? 'risk' : 'warn'}`}>
            {signal.severity === 'critical' ? 'Critical' : 'Warning'}
          </span>
          <strong>{a.asset_symbol ?? 'Asset'}</strong>
          <span className="text-secondary">{categoryLabel(a.category)}</span>
        </div>
        {a.article_title &&
          (a.article_url ? (
            <a href={a.article_url} target="_blank" rel="noopener noreferrer" className="news-item-title">
              {a.article_title}
            </a>
          ) : (
            <span className="news-item-title">{a.article_title}</span>
          ))}
        {a.summary && <p className="text-secondary infocus-summary">{a.summary}</p>}
        <button type="button" className="link-button" disabled={marking} onClick={() => onMarkRead(a.id)}>
          Mark read
        </button>
      </li>
    )
  }
  return (
    <li className="infocus-item">
      <div className="infocus-head">
        <span className={`pill ${signal.kind === 'allowance' ? 'risk' : 'warn'}`}>
          {signal.kind === 'allowance' ? 'Security' : 'Market'}
        </span>
        {signal.kind === 'allowance' ? (
          <button type="button" className="link-button" onClick={() => setPage('events')}>
            {signal.text}
          </button>
        ) : (
          <span>{signal.text}</span>
        )}
      </div>
    </li>
  )
}
