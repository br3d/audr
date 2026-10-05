import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../api/client', () => {
  class ApiError extends Error {
    status: number
    body: undefined
    constructor(status: number, message: string) {
      super(message)
      this.name = 'ApiError'
      this.status = status
    }
  }
  return {
    fetchAssets: vi.fn(),
    addManualAsset: vi.fn(),
    patchAsset: vi.fn(),
    ApiError,
  }
})

import AssetsPage from '../pages/AssetsPage'
import { fetchAssets, addManualAsset } from '../api/client'
import type { AssetItem, AssetsResponse } from '../api/client'

const mockFetchAssets = vi.mocked(fetchAssets)
const mockAddManualAsset = vi.mocked(addManualAsset)

// Use the native setter so React's synthetic onChange fires in jsdom.
function nativeSetValue(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
  setter?.call(input, value)
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

const ASSET_ETH: AssetItem = {
  id: 'a1',
  chain_id: 1,
  kind: 'native',
  contract_address: null,
  symbol: 'ETH',
  name: 'Ether',
  decimals: 18,
  excluded: false,
  held: true,
  metadata_source: 'catalog',
  has_metadata_conflict: false,
  created_at: '2026-01-01T00:00:00Z',
}

const ASSET_USDC: AssetItem = {
  id: 'a2',
  chain_id: 1,
  kind: 'catalog',
  contract_address: '0x2222222222222222222222222222222222222222',
  symbol: 'USDC',
  name: 'USD Coin',
  decimals: 6,
  excluded: false,
  held: true,
  metadata_source: 'catalog',
  has_metadata_conflict: false,
  created_at: '2026-01-01T00:00:00Z',
}

function makeAssetsResponse(items: AssetItem[] = [ASSET_ETH, ASSET_USDC]): AssetsResponse {
  return { items, next_cursor: null, request_id: 'r1', generated_at: '2026-01-01T00:00:00Z' }
}

function mountPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  act(() => {
    root.render(
      React.createElement(QueryClientProvider, { client: qc }, React.createElement(AssetsPage)),
    )
  })
  return { container, root }
}

describe('AssetsPage — search and exclusion filter persistence in the URL (AUD-353)', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    window.history.replaceState(null, '', '/')
    mockFetchAssets.mockResolvedValue(makeAssetsResponse())
  })

  afterEach(async () => {
    await act(async () => {
      root.unmount()
    })
    document.body.removeChild(container)
    vi.clearAllMocks()
  })

  it('writes the search text into the URL hash', async () => {
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(container.querySelector('input[type="search"]')).toBeTruthy())

    const searchInput = container.querySelector('input[type="search"]') as HTMLInputElement
    // Use native setter so React's synthetic onChange fires in jsdom.
    const nativeSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
    await act(async () => {
      nativeSetter?.call(searchInput, 'usdc')
      searchInput.dispatchEvent(new Event('input', { bubbles: true }))
    })

    expect(window.location.hash).toBe('#/assets?q=usdc')
    expect(container.textContent).toContain('USDC')
    expect(container.textContent).not.toContain('Ether')
  })

  it('restores the search text and "show excluded" toggle from the URL on mount (AC1)', async () => {
    window.history.replaceState(null, '', '#/assets?q=usdc&excluded=1')
    ;({ container, root } = mountPage())

    await vi.waitFor(() =>
      expect(container.querySelector('input[type="search"]')).toBeTruthy(),
    )
    expect(mockFetchAssets).toHaveBeenCalledWith({ excluded: true, held: true, cursor: undefined })
    const searchInput = container.querySelector('input[type="search"]') as HTMLInputElement
    expect(searchInput.value).toBe('usdc')
    const checkbox = container.querySelector('input[type="checkbox"]') as HTMLInputElement
    expect(checkbox.checked).toBe(true)
  })

  it('writes the "show excluded" toggle into the URL hash', async () => {
    ;({ container, root } = mountPage())
    await vi.waitFor(() => expect(container.querySelector('input[type="checkbox"]')).toBeTruthy())

    const checkbox = container.querySelector('input[type="checkbox"]') as HTMLInputElement
    await act(async () => {
      checkbox.click()
    })

    expect(window.location.hash).toBe('#/assets?excluded=1')
    await vi.waitFor(() =>
      expect(mockFetchAssets).toHaveBeenCalledWith({ excluded: true, held: true, cursor: undefined }),
    )
  })
})

describe('AssetsPage — held filter defaults to held-only (AUD-434)', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    window.history.replaceState(null, '', '/')
  })

  afterEach(async () => {
    await act(async () => {
      root.unmount()
    })
    document.body.removeChild(container)
    vi.clearAllMocks()
  })

  it('requests held=true by default, without opting in via the URL', async () => {
    mockFetchAssets.mockResolvedValue(makeAssetsResponse())
    ;({ container, root } = mountPage())

    await vi.waitFor(() =>
      expect(mockFetchAssets).toHaveBeenCalledWith({
        excluded: undefined,
        held: true,
        cursor: undefined,
      }),
    )
  })

  it('drops the held filter once "Show all catalog tokens" is checked', async () => {
    mockFetchAssets.mockResolvedValue(makeAssetsResponse())
    ;({ container, root } = mountPage())
    await vi.waitFor(() =>
      expect(container.querySelector('[aria-label="Filter assets"]')).toBeTruthy(),
    )

    const checkboxes = container.querySelectorAll('input[type="checkbox"]')
    const showAllToggle = checkboxes[1] as HTMLInputElement
    await act(async () => {
      showAllToggle.click()
    })

    expect(window.location.hash).toBe('#/assets?all=1')
    await vi.waitFor(() =>
      expect(mockFetchAssets).toHaveBeenCalledWith({
        excluded: undefined,
        held: undefined,
        cursor: undefined,
      }),
    )
  })

  it('shows a "run a balance scan" hint rather than a generic empty state when the held-only view is empty', async () => {
    mockFetchAssets.mockResolvedValue(makeAssetsResponse([]))
    ;({ container, root } = mountPage())

    await vi.waitFor(() => expect(container.textContent).toContain('No held assets yet'))
    expect(container.textContent).toContain('Run a balance scan')
  })

  it('reveals the full catalog after a manual contract is added, so the new row is not hidden by the held filter', async () => {
    mockFetchAssets.mockResolvedValue(makeAssetsResponse())
    mockAddManualAsset.mockResolvedValue({} as never)
    ;({ container, root } = mountPage())
    await vi.waitFor(() =>
      expect(container.querySelector('[aria-label="Filter assets"]')).toBeTruthy(),
    )

    const addButton = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Add contract'),
    ) as HTMLButtonElement
    await act(async () => {
      addButton.click()
    })

    const addressInput = document.querySelector('#contract-address') as HTMLInputElement
    await act(async () => {
      nativeSetValue(addressInput, '0x3333333333333333333333333333333333333333')
    })
    const form = addressInput.closest('form') as HTMLFormElement
    await act(async () => {
      form.requestSubmit()
    })

    await vi.waitFor(() => expect(mockAddManualAsset).toHaveBeenCalled())
    await vi.waitFor(() => expect(window.location.hash).toBe('#/assets?all=1'))
    await vi.waitFor(() =>
      expect(mockFetchAssets).toHaveBeenCalledWith({
        excluded: undefined,
        held: undefined,
        cursor: undefined,
      }),
    )
  })
})
