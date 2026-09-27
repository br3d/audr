const BASE = '/api/v1'

let _csrfToken: string | null = null

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

// --- API response types ---

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

// --- API functions ---

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
