import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  ApiError,
  AuthError,
  fetchSetupStatus,
  setup,
  login,
  fetchSession,
  logout,
  getCSRFToken,
  clearCSRFToken,
} from '../src/api/client'

type MockResponse = {
  ok: boolean
  status: number
  json: () => Promise<unknown>
}

function mockFetch(status: number, body: unknown): ReturnType<typeof vi.fn> {
  return vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } satisfies MockResponse)
}

beforeEach(() => {
  clearCSRFToken()
})

describe('fetchSetupStatus', () => {
  it('requests GET /api/v1/setup/status', async () => {
    const fetchMock = mockFetch(200, { setup_required: true })
    vi.stubGlobal('fetch', fetchMock)

    const result = await fetchSetupStatus()

    expect(fetchMock).toHaveBeenCalledOnce()
    const [url] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/setup/status')
    expect(result.setup_required).toBe(true)

    vi.unstubAllGlobals()
  })

  it('does not send a CSRF header on GET', async () => {
    const fetchMock = mockFetch(200, { setup_required: false })
    vi.stubGlobal('fetch', fetchMock)

    await fetchSetupStatus()

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect((init.headers as Record<string, string>)?.['X-CSRF-Token']).toBeUndefined()

    vi.unstubAllGlobals()
  })
})

describe('setup', () => {
  it('posts to /api/v1/setup and stores the CSRF token', async () => {
    const fetchMock = mockFetch(201, { csrf_token: 'token-abc' })
    vi.stubGlobal('fetch', fetchMock)

    const result = await setup('correct-horse-battery-staple')

    expect(fetchMock).toHaveBeenCalledOnce()
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/setup')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).toEqual({
      password: 'correct-horse-battery-staple',
    })
    expect(result.csrf_token).toBe('token-abc')
    expect(getCSRFToken()).toBe('token-abc')

    vi.unstubAllGlobals()
  })

  it('throws ApiError on 409', async () => {
    const fetchMock = mockFetch(409, {
      error: { code: 'setup_already_complete', message: 'Already set up.' },
      request_id: 'req-1',
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(setup('some-password-here')).rejects.toBeInstanceOf(ApiError)

    vi.unstubAllGlobals()
  })
})

describe('login', () => {
  it('posts to /api/v1/auth/login and stores the CSRF token', async () => {
    const fetchMock = mockFetch(200, { csrf_token: 'token-xyz' })
    vi.stubGlobal('fetch', fetchMock)

    const result = await login('my-secret-password')

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/auth/login')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).toEqual({
      password: 'my-secret-password',
    })
    expect(result.csrf_token).toBe('token-xyz')
    expect(getCSRFToken()).toBe('token-xyz')

    vi.unstubAllGlobals()
  })

  it('throws AuthError on 401', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      json: () => Promise.resolve({}),
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(login('wrong-password')).rejects.toBeInstanceOf(AuthError)
    expect(getCSRFToken()).toBeNull()

    vi.unstubAllGlobals()
  })

  it('includes CSRF header when a token is present', async () => {
    // First set a CSRF token by "logging in" once
    const firstFetch = mockFetch(200, { csrf_token: 'existing-token' })
    vi.stubGlobal('fetch', firstFetch)
    await login('first-login')
    vi.unstubAllGlobals()

    // Second call should include the token
    const secondFetch = mockFetch(200, { csrf_token: 'new-token' })
    vi.stubGlobal('fetch', secondFetch)
    await login('second-login')

    const [, init] = secondFetch.mock.calls[0] as [string, RequestInit]
    expect((init.headers as Record<string, string>)?.['X-CSRF-Token']).toBe(
      'existing-token',
    )

    vi.unstubAllGlobals()
  })
})

describe('fetchSession', () => {
  it('requests GET /api/v1/auth/session and stores the CSRF token', async () => {
    const fetchMock = mockFetch(200, {
      authenticated: true,
      expires_at: '2026-10-01T00:00:00Z',
      csrf_token: 'session-csrf',
    })
    vi.stubGlobal('fetch', fetchMock)

    const result = await fetchSession()

    const [url] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/auth/session')
    expect(result.authenticated).toBe(true)
    expect(getCSRFToken()).toBe('session-csrf')

    vi.unstubAllGlobals()
  })
})

describe('logout', () => {
  it('posts to /api/v1/auth/logout and clears the CSRF token', async () => {
    const loginFetch = mockFetch(200, { csrf_token: 'active-token' })
    vi.stubGlobal('fetch', loginFetch)
    await login('a-valid-password')
    vi.unstubAllGlobals()

    const logoutFetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 204,
      json: () => Promise.resolve(null),
    })
    vi.stubGlobal('fetch', logoutFetch)
    await logout()

    const [url, init] = logoutFetch.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/auth/logout')
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>)?.['X-CSRF-Token']).toBe(
      'active-token',
    )
    expect(getCSRFToken()).toBeNull()

    vi.unstubAllGlobals()
  })
})

describe('ApiError', () => {
  it('exposes status and message', () => {
    const body = {
      error: { code: 'not_found', message: 'Not found.' },
      request_id: 'r1',
    }
    const err = new ApiError(404, body)
    expect(err.status).toBe(404)
    expect(err.message).toBe('Not found.')
    expect(err.name).toBe('ApiError')
  })

  it('falls back to HTTP status in message when body is absent', () => {
    const err = new ApiError(503)
    expect(err.message).toBe('HTTP 503')
  })
})

describe('AuthError', () => {
  it('is an instance of ApiError with status 401', () => {
    const err = new AuthError()
    expect(err).toBeInstanceOf(ApiError)
    expect(err.status).toBe(401)
    expect(err.name).toBe('AuthError')
  })
})
