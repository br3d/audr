/**
 * Stand-in for api/client's ApiError, for use inside vi.mock('../api/client', ...)
 * factories. Components check `err instanceof ApiError` against whatever class the
 * mocked module exports, so every mock factory needs its own ApiError-shaped class —
 * importing the real one would defeat the mock. Import this dynamically inside the
 * factory (`const { MockApiError } = await import('./mockApiError')`) so it works
 * regardless of vi.mock hoisting order.
 */
export class MockApiError extends Error {
  status: number
  body: undefined

  constructor(status = 500, message = 'API error') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = undefined
  }
}
