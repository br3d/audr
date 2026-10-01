const BASE = '/api/v1'

let _csrfToken: string | null = null
let _unauthorizedCallback: (() => void) | null = null

// --- Error types ---

export interface ApiErrorBody {
  error: {
    code: string
    message: string
    field_errors?: Record<string, string>
    retryable?: boolean
  }
  request_id: string
}

export class ApiError extends Error {
  readonly status: number
  readonly body: ApiErrorBody | undefined

  constructor(status: number, body?: ApiErrorBody) {
    super(body?.error?.message ?? `HTTP ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

export class AuthError extends ApiError {
  constructor() {
    super(401)
    this.name = 'AuthError'
  }
}

// --- CSRF token management ---

export function setCSRFToken(token: string): void {
  _csrfToken = token
}

export function clearCSRFToken(): void {
  _csrfToken = null
}

export function getCSRFToken(): string | null {
  return _csrfToken
}

export function setUnauthorizedCallback(fn: () => void): void {
  _unauthorizedCallback = fn
}

export function clearUnauthorizedCallback(): void {
  _unauthorizedCallback = null
}

// --- Internal fetch wrapper ---

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const headers: Record<string, string> = {}

  if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
  }

  const isMutation = method !== 'GET' && method !== 'HEAD'
  if (isMutation && _csrfToken !== null) {
    headers['X-CSRF-Token'] = _csrfToken
  }

  const response = await fetch(`${BASE}${path}`, {
    method,
    headers,
    credentials: 'same-origin',
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })

  if (response.status === 401) {
    clearCSRFToken()
    _unauthorizedCallback?.()
    throw new AuthError()
  }

  if (!response.ok) {
    let errorBody: ApiErrorBody | undefined
    try {
      errorBody = (await response.json()) as ApiErrorBody
    } catch {
      // ignore parse failure — body may not be JSON
    }
    throw new ApiError(response.status, errorBody)
  }

  if (response.status === 204) {
    return undefined as T
  }

  return response.json() as Promise<T>
}

function get<T>(path: string): Promise<T> {
  return request<T>('GET', path)
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>('POST', path, body)
}

function put<T>(path: string, body?: unknown): Promise<T> {
  return request<T>('PUT', path, body)
}

function patch<T>(path: string, body?: unknown): Promise<T> {
  return request<T>('PATCH', path, body)
}

function del<T>(path: string): Promise<T> {
  return request<T>('DELETE', path)
}

// --- Auth response types ---

export interface SetupStatusResponse {
  setup_required: boolean
}

export interface SetupResponse {
  csrf_token: string
}

export interface LoginResponse {
  csrf_token: string
}

export interface SessionResponse {
  authenticated: true
  expires_at: string
  csrf_token: string
}

// --- Integrations types ---

export interface IntegrationHealth {
  status: 'ok' | 'error' | 'unvalidated' | 'validating'
  last_checked_at: string | null
  error_message: string | null
}

export interface IntegrationEntry {
  kind: string
  configured: boolean
  enabled: boolean
  provider: string | null
  host_label: string | null
  revision: string
  health: IntegrationHealth
}

export interface IntegrationsResponse {
  items: IntegrationEntry[]
  request_id: string
  generated_at: string
}

export interface JobRef {
  run_id: string
  coalesced: boolean
}

// --- Wallet types ---

export interface WalletCoverage {
  catalog_attempted: number | null
  catalog_total: number | null
  completed_at: string | null
  status: string
}

export interface WalletItem {
  id: string
  address: string
  label: string | null
  chain_id: number
  tracking_active: boolean
  coverage: WalletCoverage | null
  created_at: string
}

export interface WalletsResponse {
  items: WalletItem[]
  next_cursor: string | null
  request_id: string
  generated_at: string
}

// --- Asset types ---

export type AssetKind = 'native' | 'catalog' | 'manual' | 'discovered'
export type MetadataSource = 'catalog' | 'chain' | 'owner'

export interface AssetItem {
  id: string
  chain_id: number
  kind: AssetKind
  contract_address: string | null
  symbol: string
  name: string | null
  decimals: number | null
  excluded: boolean
  metadata_source: MetadataSource
  has_metadata_conflict: boolean
  created_at: string
}

export interface AssetsResponse {
  items: AssetItem[]
  next_cursor: string | null
  request_id: string
  generated_at: string
}

// --- Holdings types ---

export type ReadStatus = 'ok' | 'error' | 'pending' | 'stale'

export interface Holding {
  wallet_id: string
  asset_id: string
  contract_address: string | null
  is_native: boolean
  raw_balance: string | null
  decimals: number | null
  quantity: string | null
  price_usd: string | null
  value_usd: string | null
  included: boolean
  metadata_source: MetadataSource
  read_status: ReadStatus
  block_time: string | null
  observed_at: string | null
  last_success_at: string | null
}

export interface AllocationItem {
  asset_id: string
  symbol: string
  value_usd: string
  percentage: string
}

export interface PortfolioQuality {
  incomplete: boolean
  stale_balances: boolean
  stale_prices: boolean
  mixed_observation_times: boolean
  discovery_overdue: boolean
  verification_pending: boolean
  invalidated: boolean
}

export interface PortfolioResponse {
  snapshot_id: string | null
  membership_revision: string | null
  valuation_time: string | null
  currency: 'USD'
  priced_subtotal_usd: string | null
  total_usd: string | null
  quality: PortfolioQuality
  balance_block: number | null
  balance_block_time: string | null
  balance_observed_at: string | null
  discovery_completed_at: string | null
  holdings: Holding[]
  allocations: AllocationItem[]
  stale_contribution_usd: string | null
  request_id: string
  generated_at: string
}

// --- Job types ---

export type JobKind = 'balances' | 'discovery' | 'quotes'
export type JobStatus = 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'

export interface JobRun {
  id: string
  kind: JobKind
  status: JobStatus
  started_at: string | null
  finished_at: string | null
  attempted: number
  succeeded: number
  failed: number
  error_message: string | null
  created_at: string
}

// --- Auth API ---

export function fetchSetupStatus(): Promise<SetupStatusResponse> {
  return get<SetupStatusResponse>('/setup/status')
}

export async function setup(password: string): Promise<SetupResponse> {
  const result = await post<SetupResponse>('/setup', { password })
  setCSRFToken(result.csrf_token)
  return result
}

export async function login(password: string): Promise<LoginResponse> {
  const result = await post<LoginResponse>('/auth/login', { password })
  setCSRFToken(result.csrf_token)
  return result
}

export async function fetchSession(): Promise<SessionResponse> {
  const result = await get<SessionResponse>('/auth/session')
  setCSRFToken(result.csrf_token)
  return result
}

export async function logout(): Promise<void> {
  await post<void>('/auth/logout')
  clearCSRFToken()
}

// --- Integrations API ---

export function fetchIntegrations(): Promise<IntegrationsResponse> {
  return get<IntegrationsResponse>('/integrations')
}

export interface UpdateRpcInput {
  revision: string
  url: string
  headers?: Record<string, string>
  allow_private_host?: boolean
}

export function updateRpc(input: UpdateRpcInput): Promise<IntegrationEntry> {
  return put<IntegrationEntry>('/integrations/rpc', input)
}

export interface UpdateQuotesInput {
  revision: string
  provider: string
  api_key?: string
}

export function updateQuotes(input: UpdateQuotesInput): Promise<IntegrationEntry> {
  return put<IntegrationEntry>('/integrations/quotes', input)
}

export function validateIntegration(kind: 'rpc' | 'quotes'): Promise<JobRef> {
  return post<JobRef>(`/integrations/${kind}/validate`)
}

// --- Wallets API ---

export function fetchWallets(cursor?: string): Promise<WalletsResponse> {
  const params = cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''
  return get<WalletsResponse>(`/wallets${params}`)
}

export interface AddWalletInput {
  address: string
  label?: string
  chain_id: 1
}

export function addWallet(input: AddWalletInput): Promise<WalletItem> {
  return post<WalletItem>('/wallets', input)
}

export interface PatchWalletInput {
  label?: string | null
  tracking_active?: boolean
}

export function patchWallet(id: string, input: PatchWalletInput): Promise<WalletItem> {
  return patch<WalletItem>(`/wallets/${id}`, input)
}

export interface DeleteWalletResponse {
  wallet_id: string
  deleted: Record<string, number>
}

/** Permanently remove a wallet and every record derived from it (AUD-367). */
export function deleteWallet(id: string): Promise<DeleteWalletResponse> {
  return del<DeleteWalletResponse>(`/wallets/${id}`)
}

// --- Assets API ---

export function fetchAssets(excluded?: boolean, cursor?: string): Promise<AssetsResponse> {
  const params = new URLSearchParams()
  if (excluded !== undefined) params.set('excluded', String(excluded))
  if (cursor) params.set('cursor', cursor)
  const qs = params.toString()
  return get<AssetsResponse>(`/assets${qs ? '?' + qs : ''}`)
}

export interface AddManualAssetInput {
  contract_address: string
  decimals_override?: number
  symbol_override?: string
}

export function addManualAsset(input: AddManualAssetInput): Promise<AssetItem> {
  return post<AssetItem>('/assets/manual', input)
}

export interface PatchAssetInput {
  excluded?: boolean
  decimals_override?: number
  confirm_metadata_override?: boolean
}

export function patchAsset(id: string, input: PatchAssetInput): Promise<AssetItem> {
  return patch<AssetItem>(`/assets/${id}`, input)
}

// --- Portfolio API ---

export function fetchPortfolio(walletId?: string): Promise<PortfolioResponse> {
  const params = walletId ? `?wallet_id=${encodeURIComponent(walletId)}` : ''
  return get<PortfolioResponse>(`/portfolio${params}`)
}

// --- Settings types ---

export interface ScheduleConfig {
  enabled: boolean
  interval_seconds: number
  freshness_seconds?: number
  next_due_at?: string | null
}

export interface SchedulesConfig {
  balances?: ScheduleConfig
  discovery?: ScheduleConfig
  quotes?: ScheduleConfig
}

export interface SettingsResponse {
  revision: string
  schedules?: SchedulesConfig
  request_id?: string
  generated_at?: string
}

export interface PatchSettingsInput {
  revision: string
  schedules?: SchedulesConfig
}

// --- Status types ---

export interface DbStatus {
  status: 'ok' | 'error' | 'degraded'
}

export interface WorkerStatus {
  status: 'running' | 'stopped' | 'unknown'
  last_heartbeat_at: string | null
}

export interface RecoveryStatus {
  active: boolean
  reason?: string | null
}

export interface StatusResponse {
  db?: DbStatus
  worker?: WorkerStatus
  recovery?: RecoveryStatus
  schedules?: SchedulesConfig
  version?: string | null
  request_id?: string
  generated_at?: string
}

// --- Jobs list response ---

export interface JobsResponse {
  items: JobRun[]
  next_cursor: string | null
  request_id: string
  generated_at: string
}

// --- Export types ---

export interface ExportProgressEvent {
  type: 'progress' | 'complete' | 'error'
  records_written?: number
  message?: string
}

// --- Purge types ---

export interface PurgePreviewResponse {
  provider: string
  quote_observation_count: number
  quote_set_count: number
  affected_valuation_count: number
}

export interface PurgeInput {
  provider: string
  confirm: true
  current_password: string
}

// --- Jobs API ---

export function triggerJob(kind: JobKind): Promise<JobRef> {
  return post<JobRef>('/jobs', { kind })
}

export function fetchJob(id: string): Promise<JobRun> {
  return get<JobRun>(`/jobs/${id}`)
}

export function fetchJobs(kind?: JobKind, cursor?: string): Promise<JobsResponse> {
  const params = new URLSearchParams()
  if (kind) params.set('kind', kind)
  if (cursor) params.set('cursor', cursor)
  const qs = params.toString()
  return get<JobsResponse>(`/jobs${qs ? '?' + qs : ''}`)
}

export function cancelJob(id: string): Promise<void> {
  return post<void>(`/jobs/${id}/cancel`)
}

// --- Settings API ---

export function fetchSettings(): Promise<SettingsResponse> {
  return get<SettingsResponse>('/settings')
}

export function patchSettings(input: PatchSettingsInput): Promise<SettingsResponse> {
  return request<SettingsResponse>('PATCH', '/settings', input)
}

// --- Status API ---

export function fetchStatus(): Promise<StatusResponse> {
  return get<StatusResponse>('/status')
}

// --- Password API ---

export function changePassword(
  currentPassword: string,
  newPassword: string,
): Promise<void> {
  return patch<void>('/auth/password', {
    current_password: currentPassword,
    new_password: newPassword,
  })
}

// --- Export API ---

export function exportPortfolioUrl(format: 'json' | 'csv'): string {
  return `${BASE}/exports/portfolio?format=${format}`
}

export function exportHistoryUrl(
  format: 'json' | 'csv',
  from?: string,
  to?: string,
): string {
  const params = new URLSearchParams({ format })
  if (from) params.set('from', from)
  if (to) params.set('to', to)
  return `${BASE}/exports/history?${params.toString()}`
}

// --- Purge API ---

export function fetchPurgePreview(provider: string): Promise<PurgePreviewResponse> {
  return get<PurgePreviewResponse>(
    `/data/provider-purge-preview?provider=${encodeURIComponent(provider)}`,
  )
}

export function submitPurge(input: PurgeInput): Promise<JobRef> {
  return post<JobRef>('/data/provider-purge', input)
}

// --- History types ---

export type HistoryPeriod = '24h' | '7d' | '30d' | 'all'

export type HistoryQuality = 'ok' | 'stale' | 'incomplete'

export interface HistoryPoint {
  snapshot_id: string | null
  snapshotted_at: string
  total_value_usd: string | null
  quality: HistoryQuality
  included_wallet_count: number
  included_asset_count: number
  has_gap: boolean
  is_canonical: boolean
  is_gap_marker: boolean
}

export interface HistoryResponse {
  period: HistoryPeriod
  entries: HistoryPoint[]
  next_cursor: string | null
}

// --- History API ---

export function fetchHistory(period: HistoryPeriod, cursor?: string): Promise<HistoryResponse> {
  const params = new URLSearchParams({ period })
  if (cursor) params.set('cursor', cursor)
  return get<HistoryResponse>(`/history?${params.toString()}`)
}

// --- Asset news types ---

export interface AssetNewsItem {
  id: string
  source: string
  title: string
  url: string
  news_site: string
  thumbnail_url: string | null
  published_at: string
  fetched_at: string
}

export interface AssetNewsResponse {
  asset_id: string
  total: number
  limit: number
  offset: number
  news: AssetNewsItem[]
}

// --- Asset news API ---

export function fetchAssetNews(
  assetId: string,
  limit: number,
  offset: number,
): Promise<AssetNewsResponse> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  return get<AssetNewsResponse>(`/assets/${assetId}/news?${params.toString()}`)
}

// --- Events & allowances types ---

export type OnchainEventType = 'transfer_in' | 'transfer_out'

export interface OnchainEvent {
  id: string
  wallet_id: string
  tx_hash: string
  block_number: number
  log_index: number
  event_type: OnchainEventType
  token_address: string
  from_address: string
  to_address: string
  // Raw uint256 as a decimal string — never a float
  raw_amount: string
  indexed_at: string
}

export interface EventsResponse {
  total: number
  limit: number
  offset: number
  events: OnchainEvent[]
}

export interface Allowance {
  wallet_id: string
  token_address: string
  spender_address: string
  // Raw uint256 as a decimal string — never a float
  raw_amount: string
  is_unlimited: boolean
  observed_at_block: number
  tx_hash: string
  indexed_at: string
}

export interface AllowancesResponse {
  total: number
  limit: number
  offset: number
  allowances: Allowance[]
}

// --- Events & allowances API ---

export interface FetchEventsParams {
  walletId?: string
  eventType?: OnchainEventType
  tokenAddress?: string
  limit?: number
  offset?: number
}

export function fetchEvents(params: FetchEventsParams = {}): Promise<EventsResponse> {
  const q = new URLSearchParams()
  if (params.walletId) q.set('wallet_id', params.walletId)
  if (params.eventType) q.set('event_type', params.eventType)
  if (params.tokenAddress) q.set('token_address', params.tokenAddress)
  q.set('limit', String(params.limit ?? 50))
  q.set('offset', String(params.offset ?? 0))
  return get<EventsResponse>(`/events?${q.toString()}`)
}

export interface FetchAllowancesParams {
  walletId?: string
  unlimitedOnly?: boolean
  limit?: number
  offset?: number
}

export function fetchAllowances(params: FetchAllowancesParams = {}): Promise<AllowancesResponse> {
  const q = new URLSearchParams()
  if (params.walletId) q.set('wallet_id', params.walletId)
  if (params.unlimitedOnly) q.set('unlimited_only', 'true')
  q.set('limit', String(params.limit ?? 50))
  q.set('offset', String(params.offset ?? 0))
  return get<AllowancesResponse>(`/allowances?${q.toString()}`)
}
