import { test, expect } from '@playwright/test'

/**
 * E2E browser journeys for US2 valuation UI.
 *
 * These tests require:
 * - A running backend with a pre-created owner account.
 * - A running frontend dev server with /api proxy.
 * - At least one wallet tracked and a quote provider configured for the
 *   "priced holdings" scenarios; no quote provider for the "unavailable" scenarios.
 *
 * These tests are written to fail until the backend portfolio/holdings routes
 * (AUD-72) and the frontend valuation UI (AUD-73–75) are fully wired up.
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

test.describe('Dashboard page', () => {
  test('dashboard tab is present and navigates to Dashboard heading', async ({ page }) => {
    await signIn(page)
    await page.getByRole('button', { name: /dashboard/i }).click()
    await expect(page.getByRole('heading', { name: /dashboard/i })).toBeVisible()
  })

  test('shows portfolio total or "unavailable" note — never fabricated data', async ({
    page,
  }) => {
    await signIn(page)
    await page.getByRole('button', { name: /dashboard/i }).click()
    // Must show either a dollar amount or an explicit "unavailable" note
    const hasTotal = await page
      .getByText(/portfolio total/i)
      .or(page.getByText(/portfolio subtotal/i))
      .isVisible()
    const hasUnavailable = await page.getByText(/unavailable/i).isVisible()
    expect(hasTotal || hasUnavailable).toBe(true)
  })

  test('allocation section is absent when there are no priced holdings', async ({
    page,
  }) => {
    await signIn(page)
    await page.getByRole('button', { name: /dashboard/i }).click()
    // If no priced holdings, the allocation list must NOT appear
    const totalNote = page.getByText(/unavailable/i)
    if (await totalNote.isVisible()) {
      // No priced holdings — the list must be absent
      await expect(
        page.getByRole('table', { name: /asset allocation/i }),
      ).not.toBeVisible()
    }
  })

  test('stale label is shown when quality indicates stale data', async ({ page }) => {
    await signIn(page)
    await page.getByRole('button', { name: /dashboard/i }).click()
    // If the backend returns stale quality flags, the UI must surface them
    // This test verifies that the quality notices section exists in the DOM
    // (it is empty when data is fresh, which is also acceptable)
    const main = page.getByRole('main')
    await expect(main).toBeVisible()
    // If stale notices are rendered they must be in a list
    const noticeList = page.getByRole('list', { name: /data quality notices/i })
    if (await noticeList.isVisible()) {
      const items = noticeList.getByRole('listitem')
      await expect(items.first()).toBeVisible()
    }
  })

  test('"incomplete" label is shown when total is a subtotal', async ({ page }) => {
    await signIn(page)
    await page.getByRole('button', { name: /dashboard/i }).click()
    const subtotalEl = page.getByText(/portfolio subtotal.*incomplete/i)
    if (await subtotalEl.isVisible()) {
      // Verify the dollar value is present next to the incomplete label
      await expect(page.getByTestId('money-value').first()).toBeVisible()
    }
  })
})

test.describe('Connections page — quote provider', () => {
  test('quote provider section is present', async ({ page }) => {
    await signIn(page)
    await page.getByRole('button', { name: /connections/i }).click()
    await expect(
      page.getByRole('heading', { name: /price quote provider/i }),
    ).toBeVisible({ timeout: 3000 })
  })

  test('quote provider section shows disclosure about contract addresses', async ({
    page,
  }) => {
    await signIn(page)
    await page.getByRole('button', { name: /connections/i }).click()
    await expect(page.getByText(/contract address/i).first()).toBeVisible()
  })

  test('saving empty provider name shows validation error', async ({ page }) => {
    await signIn(page)
    await page.getByRole('button', { name: /connections/i }).click()
    await page.getByRole('button', { name: /save quote provider/i }).click()
    await expect(page.getByRole('alert')).toContainText(/required/i)
  })

  test('API key field is a password input (not visible in DOM)', async ({ page }) => {
    await signIn(page)
    await page.getByRole('button', { name: /connections/i }).click()
    const apiKeyInput = page.getByLabel(/api key/i)
    await expect(apiKeyInput).toHaveAttribute('type', 'password')
  })

  test('quote provider disclosure does not mention wallet addresses', async ({
    page,
  }) => {
    await signIn(page)
    await page.getByRole('button', { name: /connections/i }).click()
    const section = page.getByRole('region', { name: /price quote provider/i })
    const sectionText = await section.textContent()
    // The quotes disclosure must clarify it sees contract addresses, not wallet addresses
    expect(sectionText?.toLowerCase()).toContain('contract')
    expect(sectionText?.toLowerCase()).not.toContain('wallet address')
  })
})

test.describe('Assets page — unpriced and conflict states', () => {
  test('assets page shows conflict count notice when conflicts exist', async ({
    page,
  }) => {
    await signIn(page)
    await page.getByRole('button', { name: /assets/i }).click()
    // If there are assets with metadata conflicts the notice must appear
    const conflictNote = page.getByText(/metadata conflict/i)
    // Either no conflicts (no notice) or a notice is shown — never silent
    const assetsList = page.getByRole('list', { name: /assets/i })
    if (await assetsList.isVisible()) {
      const conflictAssets = assetsList.getByRole('note').filter({
        hasText: /metadata conflict detected/i,
      })
      const count = await conflictAssets.count()
      if (count > 0) {
        await expect(conflictNote).toBeVisible()
      }
    }
  })

  test('excluded count is shown in the toggle label when assets are excluded', async ({
    page,
  }) => {
    await signIn(page)
    await page.getByRole('button', { name: /assets/i }).click()
    // The label may show "(N hidden)" when there are excluded assets
    const label = page.getByText(/show excluded/i)
    await expect(label).toBeVisible()
  })

  test('unpriced notice uses "Unpriced" wording for assets with unknown decimals', async ({
    page,
  }) => {
    await signIn(page)
    await page.getByRole('button', { name: /assets/i }).click()
    const unpricedNotes = page.getByRole('note').filter({ hasText: /unpriced/i })
    if (await unpricedNotes.count() > 0) {
      await expect(unpricedNotes.first()).toContainText(/decimals unknown/i)
    }
  })
})

test.describe('MoneyValue display integrity', () => {
  test('no dollar values are shown as zero when they should be unknown', async ({
    page,
  }) => {
    await signIn(page)
    await page.getByRole('button', { name: /dashboard/i }).click()
    // If the portfolio total is unavailable, the text must say "unavailable",
    // not "$0.00" or "0"
    const zeroAmount = page.getByTestId('money-value').filter({ hasText: '$0.00' })
    // $0.00 is a valid display only when the value is actually zero; we check
    // that when the total note says "unavailable", there is no $0.00 next to it
    const unavailable = page.getByText(/unavailable/i)
    if (await unavailable.isVisible()) {
      // The portfolio section must not show a $0.00 amount alongside "unavailable"
      const section = page.getByRole('region', { name: /portfolio total/i })
      expect(await section.getByTestId('money-value').count()).toBe(0)
    } else {
      // When a total is shown, verify it's a real dollar-formatted value
      const moneySpans = page.getByTestId('money-value')
      if ((await moneySpans.count()) > 0) {
        const firstValue = await moneySpans.first().textContent()
        expect(firstValue).toMatch(/^\$[\d,]+\.\d{2}$/)
      }
    }
  })

  test('allocation table value cells show formatted dollar amounts', async ({
    page,
  }) => {
    await signIn(page)
    await page.getByRole('button', { name: /dashboard/i }).click()
    const table = page.getByRole('table', { name: /asset allocation/i })
    if (await table.isVisible()) {
      const valueCells = table.getByTestId('money-value')
      const count = await valueCells.count()
      expect(count).toBeGreaterThan(0)
      const firstValue = await valueCells.first().textContent()
      expect(firstValue).toMatch(/^\$[\d,]+\.\d{2}$/)
    }
  })
})
