import { describe, it, expect, vi, afterEach } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../api/client', async () => {
  const { MockApiError } = await import('./mockApiError')
  return {
    fetchSettings: vi.fn(),
    patchSettings: vi.fn(),
    fetchNewsAnalysisSetting: vi.fn(),
    patchNewsAnalysisSetting: vi.fn(),
    fetchIntegrations: vi.fn(),
    ApiError: MockApiError,
  }
})

import SchedulesPage from '../pages/SchedulesPage'
import {
  fetchSettings,
  fetchNewsAnalysisSetting,
  patchNewsAnalysisSetting,
  fetchIntegrations,
} from '../api/client'
import type { SettingsResponse, NewsAnalysisSetting, IntegrationsResponse } from '../api/client'

const mockFetchSettings = vi.mocked(fetchSettings)
const mockFetchNewsAnalysisSetting = vi.mocked(fetchNewsAnalysisSetting)
const mockPatchNewsAnalysisSetting = vi.mocked(patchNewsAnalysisSetting)
const mockFetchIntegrations = vi.mocked(fetchIntegrations)

function baseSettings(): SettingsResponse {
  return { revision: '1', schedules: {} }
}

function baseNewsAnalysis(overrides: Partial<NewsAnalysisSetting> = {}): NewsAnalysisSetting {
  return { enabled: false, importance_threshold: 50, revision: 1, ...overrides }
}

function baseIntegrations(llmConfigured: boolean): IntegrationsResponse {
  const health = { status: 'unvalidated' as const, last_checked_at: null, error_message: null }
  return {
    items: [
      {
        kind: 'llm',
        configured: llmConfigured,
        enabled: true,
        provider: 'anthropic',
        host_label: null,
        revision: '0',
        health,
        effective_source: llmConfigured ? 'Anthropic' : null,
        using_default: false,
        options: [],
      },
    ],
    request_id: 'r1',
    generated_at: '2026-01-01T00:00:00Z',
  }
}

function mountPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  act(() => {
    root.render(
      React.createElement(QueryClientProvider, { client: qc }, React.createElement(SchedulesPage)),
    )
  })
  return { container, root }
}

function buttonByText(container: HTMLElement, text: string): HTMLButtonElement {
  const match = Array.from(container.querySelectorAll('button')).find((b) =>
    (b.textContent ?? '').includes(text),
  )
  if (!match) throw new Error(`no button matching ${text}`)
  return match as HTMLButtonElement
}

describe('SchedulesPage — smart news analysis card (AUD-659)', () => {
  let container: HTMLDivElement
  let root: Root

  afterEach(async () => {
    await act(async () => {
      root.unmount()
    })
    document.body.removeChild(container)
    vi.clearAllMocks()
  })

  it('shows the checkbox off and no threshold control when disabled (unchanged interface)', async () => {
    mockFetchSettings.mockResolvedValue(baseSettings())
    mockFetchNewsAnalysisSetting.mockResolvedValue(baseNewsAnalysis({ enabled: false }))
    mockFetchIntegrations.mockResolvedValue(baseIntegrations(false))
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(container.querySelector('#news-analysis-enabled')).toBeTruthy())

    const checkbox = container.querySelector('#news-analysis-enabled') as HTMLInputElement
    expect(checkbox.checked).toBe(false)
    expect(container.querySelector('#news-analysis-threshold')).toBeNull()
    expect(container.textContent).not.toMatch(/no anthropic key is configured/i)
  })

  it('reveals the importance threshold slider once enabled', async () => {
    mockFetchSettings.mockResolvedValue(baseSettings())
    mockFetchNewsAnalysisSetting.mockResolvedValue(baseNewsAnalysis({ enabled: true, importance_threshold: 60 }))
    mockFetchIntegrations.mockResolvedValue(baseIntegrations(true))
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(container.querySelector('#news-analysis-enabled')).toBeTruthy())

    const slider = container.querySelector('#news-analysis-threshold') as HTMLInputElement
    expect(slider).toBeTruthy()
    expect(slider.type).toBe('range')
    expect(slider.value).toBe('60')
  })

  it('warns explicitly when enabled but no Anthropic key is configured', async () => {
    mockFetchSettings.mockResolvedValue(baseSettings())
    mockFetchNewsAnalysisSetting.mockResolvedValue(baseNewsAnalysis({ enabled: true }))
    mockFetchIntegrations.mockResolvedValue(baseIntegrations(false))
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(container.querySelector('#news-analysis-enabled')).toBeTruthy())

    expect(container.textContent).toMatch(/no anthropic key is configured/i)
  })

  it('does not warn when enabled and a key is configured', async () => {
    mockFetchSettings.mockResolvedValue(baseSettings())
    mockFetchNewsAnalysisSetting.mockResolvedValue(baseNewsAnalysis({ enabled: true }))
    mockFetchIntegrations.mockResolvedValue(baseIntegrations(true))
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(container.querySelector('#news-analysis-enabled')).toBeTruthy())

    expect(container.textContent).not.toMatch(/no anthropic key is configured/i)
  })

  it('saves the toggle and threshold together, revision-checked', async () => {
    mockFetchSettings.mockResolvedValue(baseSettings())
    mockFetchNewsAnalysisSetting.mockResolvedValue(baseNewsAnalysis({ enabled: false, revision: 7 }))
    mockFetchIntegrations.mockResolvedValue(baseIntegrations(false))
    mockPatchNewsAnalysisSetting.mockResolvedValue(
      baseNewsAnalysis({ enabled: true, importance_threshold: 70, revision: 8 }),
    )
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(container.querySelector('#news-analysis-enabled')).toBeTruthy())

    const checkbox = container.querySelector('#news-analysis-enabled') as HTMLInputElement
    await act(async () => {
      checkbox.click()
    })

    const slider = container.querySelector('#news-analysis-threshold') as HTMLInputElement
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
      setter?.call(slider, '70')
      slider.dispatchEvent(new Event('change', { bubbles: true }))
    })

    await act(async () => {
      buttonByText(container, 'Save smart analysis settings').click()
    })

    expect(mockPatchNewsAnalysisSetting).toHaveBeenCalledWith({
      enabled: true,
      importance_threshold: 70,
      expected_revision: 7,
    })
  })
})
