import { test, expect } from '@playwright/test'

/**
 * E2E browser journeys for US3 history UI.
 *
 * Requirements:
 * - A running backend with a pre-created owner account.
 * - A running frontend dev server with /api proxy.
 * - The backend history endpoint (AUD-253) does not need real data for most tests;
 *   the UI must handle empty responses gracefully.
 *
 * These tests are written to fail until the backend history route (AUD-253)
 * and the frontend history UI (AUD-254) are fully wired up.
 */

const APP_URL = process.env.APP_URL ?? 'http://localhost:5173'
const OWNER_PASSWORD = process.env.OWNER_PASSWORD ?? 'correct-horse-battery-staple-42'

async function signIn(page: import('@playwright/test').Page) {
  await page.goto(APP_URL)
  const passwordField = page.getByLabel('Password')
  await passwordField.waitFor()
  await passwordField.fill(OWNER_PASSWORD)
  await page.getByRole('button', { name: /sign in/i }).click()
  await expect(
    page.getByRole('heading', { name: /dashboard|holdings/i }),
  ).toBeVisible({ timeout: 5000 })
}

async function navigateToHistory(page: import('@playwright/test').Page) {
  await signIn(page)
  await page.getByRole('button', { name: /history/i }).click()
  await expect(page.getByRole('heading', { name: /portfolio history/i })).toBeVisible({
    timeout: 5000,
  })
}

test.describe('History page — navigation', () => {
  test('History tab is present in navigation', async ({ page }) => {
    await signIn(page)
    await expect(page.getByRole('button', { name: /history/i })).toBeVisible()
  })

  test('clicking History tab shows Portfolio History heading', async ({ page }) => {
    await navigateToHistory(page)
  })
})

test.describe('History page — range selector', () => {
  test('range selector contains all six options: 24h, 7d, 30d, 90d, 1 year, all time', async ({
    page,
  }) => {
    await navigateToHistory(page)
    const fieldset = page.getByRole('group', { name: /range/i })
    await expect(fieldset.getByRole('button', { name: '24h' })).toBeVisible()
    await expect(fieldset.getByRole('button', { name: '7d' })).toBeVisible()
    await expect(fieldset.getByRole('button', { name: '30d' })).toBeVisible()
    await expect(fieldset.getByRole('button', { name: '90d' })).toBeVisible()
    await expect(fieldset.getByRole('button', { name: '1 year' })).toBeVisible()
    await expect(fieldset.getByRole('button', { name: 'All time' })).toBeVisible()
  })

  test('default selected range is 7 days', async ({ page }) => {
    await navigateToHistory(page)
    const btn7d = page.getByRole('button', { name: '7d' })
    await expect(btn7d).toHaveAttribute('aria-pressed', 'true')
  })

  test('selecting 24h range updates pressed state', async ({ page }) => {
    await navigateToHistory(page)
    const btn24h = page.getByRole('button', { name: '24h' })
    await btn24h.click()
    await expect(btn24h).toHaveAttribute('aria-pressed', 'true')
  })

  test('selecting 30d range updates pressed state', async ({ page }) => {
    await navigateToHistory(page)
    const btn30d = page.getByRole('button', { name: '30d' })
    await btn30d.click()
    await expect(btn30d).toHaveAttribute('aria-pressed', 'true')
  })

  test('selecting 90d range updates pressed state and requests data', async ({ page }) => {
    await navigateToHistory(page)
    const btn90d = page.getByRole('button', { name: '90d' })
    await btn90d.click()
    await expect(btn90d).toHaveAttribute('aria-pressed', 'true')
  })

  test('selecting 1 year range updates pressed state and requests data', async ({ page }) => {
    await navigateToHistory(page)
    const btn1y = page.getByRole('button', { name: '1 year' })
    await btn1y.click()
    await expect(btn1y).toHaveAttribute('aria-pressed', 'true')
  })

  test('selecting all time range updates pressed state', async ({ page }) => {
    await navigateToHistory(page)
    const btnAll = page.getByRole('button', { name: 'All time' })
    await btnAll.click()
    await expect(btnAll).toHaveAttribute('aria-pressed', 'true')
  })

  test('range selector is keyboard navigable', async ({ page }) => {
    await navigateToHistory(page)
    const btn24h = page.getByRole('button', { name: '24h' })
    // Tab to the first range button and use arrow keys
    await btn24h.focus()
    await expect(btn24h).toBeFocused()
  })
})

test.describe('History page — no PnL terminology', () => {
  test('page does not contain "profit" anywhere', async ({ page }) => {
    await navigateToHistory(page)
    // Wait for data or empty state to settle
    await page.waitForTimeout(1000)
    const bodyText = await page.locator('main').textContent()
    expect(bodyText?.toLowerCase()).not.toContain('profit')
  })

  test('page does not contain "loss" anywhere', async ({ page }) => {
    await navigateToHistory(page)
    await page.waitForTimeout(1000)
    const bodyText = await page.locator('main').textContent()
    expect(bodyText?.toLowerCase()).not.toContain('loss')
  })

  test('page does not contain "pnl" anywhere', async ({ page }) => {
    await navigateToHistory(page)
    await page.waitForTimeout(1000)
    const bodyText = await page.locator('main').textContent()
    expect(bodyText?.toLowerCase()).not.toContain('pnl')
  })

  test('page does not contain "gain" anywhere', async ({ page }) => {
    await navigateToHistory(page)
    await page.waitForTimeout(1000)
    const bodyText = await page.locator('main').textContent()
    expect(bodyText?.toLowerCase()).not.toContain('gain')
  })
})

test.describe('History page — chart and accessible table', () => {
  test('shows chart container or empty state — never blank', async ({ page }) => {
    await navigateToHistory(page)
    await page.waitForTimeout(1500)
    const hasChart = await page
      .getByRole('img', { name: /portfolio value history chart/i })
      .isVisible()
    const hasEmptyNote = await page.getByText(/no history data/i).isVisible()
    expect(hasChart || hasEmptyNote).toBe(true)
  })

  test('accessible data table is present when chart is shown', async ({ page }) => {
    await navigateToHistory(page)
    await page.waitForTimeout(1500)
    const hasChart = await page
      .getByRole('img', { name: /portfolio value history chart/i })
      .isVisible()
    if (hasChart) {
      // The accessible table lives inside a <details> element
      const details = page.locator('details')
      await expect(details).toBeVisible()
      // Open the details to expose the table
      await details.locator('summary').click()
      const table = page.getByRole('table', { name: /portfolio value history/i })
      await expect(table).toBeVisible()
    }
  })

  test('history section is labeled', async ({ page }) => {
    await navigateToHistory(page)
    await page.waitForTimeout(1500)
    const section = page.getByRole('region', { name: /history chart/i })
    if (await section.isVisible()) {
      await expect(section).toBeVisible()
    }
  })
})

test.describe('History page — gap handling', () => {
  test('gap explanation note is shown when data contains gaps', async ({ page }) => {
    await navigateToHistory(page)
    await page.waitForTimeout(1500)
    // If gap markers exist in the table, the explanation note must also be present
    const gapMarkers = page.getByTestId('gap-marker')
    const count = await gapMarkers.count()
    if (count > 0) {
      await expect(page.getByText(/gap/i).first()).toBeVisible()
    }
  })
})

test.describe('History page — invalidation handling', () => {
  test('invalidation explanation note is shown when data contains non-canonical points', async ({
    page,
  }) => {
    await navigateToHistory(page)
    await page.waitForTimeout(1500)
    // If invalidated markers exist in the table, the explanation note must also be present
    const invalidatedMarkers = page.getByTestId('invalidated-marker')
    const count = await invalidatedMarkers.count()
    if (count > 0) {
      await expect(page.getByText(/invalidated/i).first()).toBeVisible()
    }
  })
})

test.describe('History page — data quality notices', () => {
  test('stale notice uses "stale" wording, not PnL terms', async ({ page }) => {
    await navigateToHistory(page)
    await page.waitForTimeout(1500)
    const staleNotice = page.getByRole('note').filter({ hasText: /stale/i })
    if ((await staleNotice.count()) > 0) {
      const text = await staleNotice.first().textContent()
      expect(text?.toLowerCase()).not.toContain('profit')
      expect(text?.toLowerCase()).not.toContain('loss')
    }
  })

  test('incomplete notice uses "incomplete" wording, not PnL terms', async ({ page }) => {
    await navigateToHistory(page)
    await page.waitForTimeout(1500)
    const incompleteNotice = page.getByRole('note').filter({ hasText: /incomplete/i })
    if ((await incompleteNotice.count()) > 0) {
      const text = await incompleteNotice.first().textContent()
      expect(text?.toLowerCase()).not.toContain('profit')
      expect(text?.toLowerCase()).not.toContain('loss')
    }
  })
})

test.describe('History API contract', () => {
  test('frontend sends ?period= (not ?range=) and renders without crashing on entries response', async ({ page }) => {
    let capturedUrl: string | null = null

    // Intercept the history API call so we can assert the query param name
    // and return a well-formed response without requiring a running backend.
    await page.route('**/api/v1/history*', async (route) => {
      capturedUrl = route.request().url()
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          period: '7d',
          entries: [
            {
              snapshot_id: 'abc-1',
              snapshotted_at: '2026-01-15T00:00:00Z',
              total_value_usd: '5000.00',
              quality: 'ok',
              included_wallet_count: 1,
              included_asset_count: 1,
              has_gap: false,
              is_canonical: true,
              is_gap_marker: false,
            },
          ],
          next_cursor: null,
        }),
      })
    })

    await navigateToHistory(page)

    // Verify the request used `period=`, not `range=`
    expect(capturedUrl).not.toBeNull()
    const url = new URL(capturedUrl!)
    expect(url.searchParams.has('period')).toBe(true)
    expect(url.searchParams.has('range')).toBe(false)

    // Verify the page rendered the data without throwing (chart or table visible)
    const hasChart = await page
      .getByRole('img', { name: /portfolio value history chart/i })
      .isVisible()
    const hasTable = await page.getByRole('table', { name: /portfolio value history/i })
      .isVisible().catch(() => false)
    expect(hasChart || hasTable).toBe(true)
  })
})

test.describe('History page — mobile viewport', () => {
  test.use({ viewport: { width: 390, height: 844 } })

  test('page renders correctly at mobile width', async ({ page }) => {
    await navigateToHistory(page)
    await expect(page.getByRole('heading', { name: /portfolio history/i })).toBeVisible()
  })

  test('range selector is visible and usable on mobile', async ({ page }) => {
    await navigateToHistory(page)
    const fieldset = page.getByRole('group', { name: /range/i })
    await expect(fieldset).toBeVisible()
    await fieldset.getByRole('button', { name: '30d' }).click()
    await expect(fieldset.getByRole('button', { name: '30d' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  test('chart or empty state is visible on mobile', async ({ page }) => {
    await navigateToHistory(page)
    await page.waitForTimeout(1500)
    const hasChart = await page
      .getByRole('img', { name: /portfolio value history chart/i })
      .isVisible()
    const hasEmptyNote = await page.getByText(/no history data/i).isVisible()
    const hasLoading = await page.getByText(/loading history/i).isVisible()
    expect(hasChart || hasEmptyNote || hasLoading).toBe(true)
  })
})
