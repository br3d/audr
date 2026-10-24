import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'

vi.mock('../api/client', async () => {
  const { MockApiError } = await import('./mockApiError')
  return {
    exportData: vi.fn(),
    ApiError: MockApiError,
  }
})

import ExportSection from '../components/accountData/ExportSection'
import { exportData } from '../api/client'

const mockExportData = vi.mocked(exportData)

function mountComponent() {
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  act(() => {
    root.render(React.createElement(ExportSection))
  })
  return { container, root }
}

function clickExportButton(container: HTMLElement) {
  const btn = container.querySelector('button[type="submit"]') as HTMLButtonElement
  btn.click()
}

function makeStreamingResponse(chunks: Uint8Array[], filename = 'audr-export.json') {
  let index = 0
  return {
    headers: {
      get: (key: string) => (key === 'Content-Disposition' ? `attachment; filename="${filename}"` : null),
    },
    body: {
      getReader: () => ({
        read: async () => {
          if (index < chunks.length) {
            const value = chunks[index]
            index += 1
            return { done: false, value }
          }
          return { done: true, value: undefined }
        },
      }),
    },
  } as unknown as Response
}

describe('ExportSection', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal('URL', {
      ...URL,
      createObjectURL: vi.fn(() => 'blob:mock-url'),
      revokeObjectURL: vi.fn(),
    })
  })

  afterEach(async () => {
    await act(async () => {
      root.unmount()
    })
    document.body.removeChild(container)
    vi.clearAllMocks()
    vi.unstubAllGlobals()
  })

  it('defaults to a portfolio/JSON export', async () => {
    ;({ container, root } = mountComponent())
    const btn = container.querySelector('button[type="submit"]') as HTMLButtonElement
    expect(btn.textContent).toBe('Export portfolio as JSON')
  })

  it('streams the response, triggers a download, and reports the final size', async () => {
    mockExportData.mockResolvedValue(makeStreamingResponse([new Uint8Array(1024), new Uint8Array(512)]))
    ;({ container, root } = mountComponent())

    await act(async () => {
      clickExportButton(container)
    })

    expect(mockExportData).toHaveBeenCalledWith('portfolio', 'json', { signal: expect.any(AbortSignal) })
    expect(container.textContent).toContain('Download complete')
    expect(URL.createObjectURL).toHaveBeenCalled()
  })

  it('switches kind and format before exporting', async () => {
    mockExportData.mockResolvedValue(makeStreamingResponse([]))
    ;({ container, root } = mountComponent())

    const historyRadio = container.querySelector(
      'input[name="export-kind"][value="history"]',
    ) as HTMLInputElement
    const csvRadio = container.querySelector('input[name="export-format"][value="csv"]') as HTMLInputElement
    await act(async () => {
      historyRadio.click()
      csvRadio.click()
    })

    await act(async () => {
      clickExportButton(container)
    })

    expect(mockExportData).toHaveBeenCalledWith('history', 'csv', { signal: expect.any(AbortSignal) })
  })

  it('shows the API error message when the export fails', async () => {
    const { MockApiError } = await import('./mockApiError')
    mockExportData.mockRejectedValue(new MockApiError(500, 'Export generation failed.'))
    ;({ container, root } = mountComponent())

    await act(async () => {
      clickExportButton(container)
    })

    expect(container.textContent).toContain('Export generation failed.')
  })

  it('returns to idle without an error message when the export is cancelled', async () => {
    let capturedSignal: AbortSignal | undefined
    let rejectExport: (err: unknown) => void = () => {}
    mockExportData.mockImplementation((_kind, _format, options) => {
      capturedSignal = options?.signal
      return new Promise((_resolve, reject) => {
        rejectExport = reject
      })
    })
    ;({ container, root } = mountComponent())

    await act(async () => {
      clickExportButton(container)
    })

    const cancelBtn = container.querySelector('button[aria-label="Cancel export"]') as HTMLButtonElement
    expect(cancelBtn).toBeTruthy()
    await act(async () => {
      cancelBtn.click()
    })

    expect(capturedSignal?.aborted).toBe(true)

    const abortError = Object.assign(new Error('aborted'), { name: 'AbortError' })
    await act(async () => {
      rejectExport(abortError)
    })

    expect(container.querySelector('button[aria-label="Cancel export"]')).toBeNull()
    expect(container.querySelector('.alert-danger')).toBeNull()
  })
})
