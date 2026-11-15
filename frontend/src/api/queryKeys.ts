import type { QueryClient } from '@tanstack/react-query'
import type { HistoryPeriod, JobKind, OnchainEventType } from './client'

/**
 * Typed query-key factory. One function per query shape — keeps every
 * `useQuery`/`useMutation` invalidation call on the same literal instead of
 * hand-typed tuples that drift (e.g. `['assets', showExcluded, showAllCatalog]`
 * vs `['assets', 'excluded-strip']` under the same `'assets'` prefix).
 */
export const queryKeys = {
  portfolio: () => ['portfolio'] as const,

  assets: (filters: { excluded: boolean; showAllCatalog: boolean }) =>
    ['assets', filters.excluded, filters.showAllCatalog] as const,
  assetsSymbolLookup: () => ['assets', 'symbol-lookup'] as const,
  assetsExcludedStrip: () => ['assets', 'excluded-strip'] as const,

  history: (period: HistoryPeriod) => ['history', period] as const,

  events: (filters: { walletId: string; eventType: OnchainEventType | ''; offset: number }) =>
    ['events', filters.walletId, filters.eventType, filters.offset] as const,
  eventsRecent: () => ['events', 'recent'] as const,

  allowances: (filters: { walletId: string; unlimitedOnly: boolean; offset: number }) =>
    ['allowances', filters.walletId, filters.unlimitedOnly, filters.offset] as const,

  walletsForEventsFilter: () => ['wallets-for-events-filter'] as const,
  wallets: () => ['wallets'] as const,

  integrations: () => ['integrations'] as const,
  settings: () => ['settings'] as const,
  newsAnalysisSetting: () => ['news-analysis-setting'] as const,
  systemStatus: () => ['system-status'] as const,

  jobs: () => ['jobs'] as const,
  job: (runId: string | null) => ['job', runId] as const,

  apiTokens: () => ['api-tokens'] as const,
  alerts: () => ['alerts'] as const,
  // Prefixed by alerts() so the bell's mark-read invalidation refreshes it too.
  alertsUnread: () => ['alerts', 'unread'] as const,
  assetNews: (assetId: string) => ['asset-news', assetId] as const,
  assetNewsLatest: (assetId: string) => ['asset-news', assetId, 'latest'] as const,
  allowancesUnlimited: () => ['allowances', 'unlimited-dashboard'] as const,
  purgePreview: (provider: string) => ['purge-preview', provider] as const,
} satisfies Record<string, (...args: never[]) => readonly unknown[]>

/**
 * Triggering a {@link JobKind} scan rewrites balances/catalog rows and
 * re-cuts the portfolio and history snapshots against them — the same trio
 * every write that moves holdings has to drop (AUD-448, AUD-446).
 */
export function invalidatePortfolioGroup(queryClient: QueryClient): void {
  void queryClient.invalidateQueries({ queryKey: queryKeys.portfolio() })
  void queryClient.invalidateQueries({ queryKey: ['assets'] })
  void queryClient.invalidateQueries({ queryKey: ['history'] })
}
