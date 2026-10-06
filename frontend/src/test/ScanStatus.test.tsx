import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { JobRun } from '../api/client'

vi.mock('../api/client', () => ({
  fetchJob: vi.fn(),
  triggerJob: vi.fn(),
  ApiError: class ApiError extends Error {
    status = 500
    body = undefined
    constructor(msg = 'API error') {
      super(msg)
      this.name = 'ApiError'
    }
  },
}))

import ScanStatus from '../components/ScanStatus'
import { fetchJob, triggerJob } from '../api/client'

const mockFetchJob = vi.mocked(fetchJob)
const mockTriggerJob = vi.mocked(triggerJob)

function makeJob(overrides: Partial<JobRun> = {}): JobRun {
  return {
    id: 'run-1',
    kind: 'balances',
    status: 'running',
    started_at: '2026-10-06T09:46:00Z',
    finished_at: null,
    attempted: 1,
    succeeded: 0,
    failed: 0,
    error_message: null,
    created_at: '2026-10-06T09:46:00Z',
    ...overrides,
  }
}

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  })
}

describe('ScanStatus', () => {
  let container: HTMLDivElement
  let root: Root | null = null

  beforeEach(() => {
    vi.clearAllMocks()
    container = document.createElement('div')
    document.body.appendChild(container)
  })

  afterEach(() => {
    act(() => {
      root?.unmount()
    })
    root = null
    container.remove()
  })

  // Seeding the job cache keeps these tests deterministic: useQuery returns the
  // row synchronously (staleTime: Infinity) instead of racing a mocked promise.
  function mount(qc: QueryClient, seed?: JobRun): void {
    if (seed !== undefined) qc.setQueryData(['job', 'run-1'], seed)
    root = createRoot(container)
    act(() => {
      root!.render(
        React.createElement(
          QueryClientProvider,
          { client: qc },
          React.createElement(ScanStatus, {
            runId: 'run-1',
            kind: 'balances',
            label: 'Refresh balances',
          }),
        ),
      )
    })
  }

  function statusText(): string {
    return container.querySelector('[role="status"]')?.textContent ?? ''
  }

  it('reports a running scan without a counter that never moves', async () => {
    // AUD-446: the API synthesises attempted/succeeded/failed from retry_count,
    // so the old "(0 / 1 processed)" read as a frozen progress bar for the
    // whole multi-minute run.
    const job = makeJob({ status: 'running' })
    mockFetchJob.mockResolvedValue(job)
    const qc = makeQueryClient()
    mount(qc, job)

    expect(statusText()).toContain('Refresh balances: running')
    expect(statusText()).not.toContain('processed')
  })

  it('refetches the portfolio views once the scan reaches a terminal state', async () => {
    // AUD-446: without this the allocation table kept rendering its cached
    // pre-scan data, so a successful refresh looked like it had done nothing
    // until the user reloaded the page by hand.
    const job = makeJob({ status: 'completed', finished_at: 'x' })
    mockFetchJob.mockResolvedValue(job)
    const qc = makeQueryClient()
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    mount(qc, job)

    const keys = invalidate.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey))
    expect(keys).toContain(JSON.stringify(['portfolio']))
    expect(keys).toContain(JSON.stringify(['assets']))
    expect(keys).toContain(JSON.stringify(['history']))
  })

  it('does not refetch while the scan is still running', async () => {
    const job = makeJob({ status: 'running' })
    mockFetchJob.mockResolvedValue(job)
    const qc = makeQueryClient()
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    mount(qc, job)

    const keys = invalidate.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey))
    expect(keys).not.toContain(JSON.stringify(['portfolio']))
  })

  it('still refetches when the scan ends in failure', async () => {
    // A failed run can have written partial balances; the cached view is stale
    // either way, and the user must not be left reading pre-scan numbers.
    const job = makeJob({ status: 'failed', error_message: 'rpc timeout', finished_at: 'x' })
    mockFetchJob.mockResolvedValue(job)
    const qc = makeQueryClient()
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    mount(qc, job)

    const keys = invalidate.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey))
    expect(keys).toContain(JSON.stringify(['portfolio']))
    expect(statusText()).toContain('rpc timeout')
  })

  it('starts a scan and tracks the returned run id', async () => {
    mockTriggerJob.mockResolvedValue({ run_id: 'run-2', coalesced: false })
    mockFetchJob.mockResolvedValue(makeJob({ id: 'run-2', status: 'completed' }))
    const qc = makeQueryClient()
    mount(qc)

    const button = container.querySelector('button')!
    await act(async () => {
      button.click()
    })
    await act(async () => {
      await Promise.resolve()
    })

    expect(mockTriggerJob).toHaveBeenCalledWith('balances')
    expect(statusText()).toContain('Refresh balances: complete')
  })
})
