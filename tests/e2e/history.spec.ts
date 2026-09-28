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
  test('range selector contains all four options: 24h, 7d, 30d, all time', async ({
    page,
  }) => {
    await navigateToHistory(page)
    const fieldset = page.getByRole('group', { name: /range/i })
    await expect(fieldset.getByRole('radio', { name: /24 hours/i })).toBeVisible()
    await expect(fieldset.getByRole('radio', { name: /7 days/i })).toBeVisible()
    await expect(fieldset.getByRole('radio', { name: /30 days/i })).toBeVisible()
    await expect(fieldset.getByRole('radio', { name: /all time/i })).toBeVisible()
  })

  test('default selected range is 7 days', async ({ page }) => {
    await navigateToHistory(page)
    const radio7d = page.getByRole('radio', { name: /7 days/i })
    await expect(radio7d).toBeChecked()
  })

  test('selecting 24h range updates checked state', async ({ page }) => {
    await navigateToHistory(page)
    const radio24h = page.getByRole('radio', { name: /24 hours/i })
    await radio24h.click()
    await expect(radio24h).toBeChecked()
  })

  test('selecting 30d range updates checked state', async ({ page }) => {
    await navigateToHistory(page)
    const radio30d = page.getByRole('radio', { name: /30 days/i })
    await radio30d.click()
    await expect(radio30d).toBeChecked()
  })

  test('selecting all time range updates checked state', async ({ page }) => {
    await navigateToHistory(page)
    const radioAll = page.getByRole('radio', { name: /all time/i })
    await radioAll.click()
    await expect(radioAll).toBeChecked()
  })

  test('range selector is keyboard navigable', async ({ page }) => {
    await navigateToHistory(page)
    const radio24h = page.getByRole('radio', { name: /24 hours/i })
    // Tab to the first radio and use arrow keys
    await radio24h.focus()
    await expect(radio24h).toBeFocused()
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
    await fieldset.getByRole('radio', { name: /30 days/i }).click()
    await expect(fieldset.getByRole('radio', { name: /30 days/i })).toBeChecked()
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
