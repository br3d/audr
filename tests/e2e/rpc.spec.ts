import { test, expect } from '@playwright/test'

/**
 * E2E browser journeys for RPC connection and wallet/address management.
 *
 * These tests require:
 * - A running backend with a pre-created owner account.
 * - A running frontend dev server with /api proxy.
 * - A controlled RPC stub that can be configured per test.
 *
 * These tests are intentionally written to fail until the backend integration
 * routes (T028–T030) and the frontend pages (T043–T045) are fully wired up.
 */

const APP_URL = process.env.APP_URL ?? 'http://localhost:5173'
const OWNER_PASSWORD = process.env.OWNER_PASSWORD ?? 'correct-horse-battery-staple-42'

async function signIn(page: import('@playwright/test').Page) {
  await page.goto(APP_URL)
  const passwordField = page.getByLabel('Password')
  await passwordField.waitFor()
  await passwordField.fill(OWNER_PASSWORD)
  await page.getByRole('button', { name: /sign in/i }).click()
  await page.waitForURL(/\/(?!$)/, { timeout: 5000 }).catch(() => {
    // some apps stay on the same URL and just change content
  })
}

test.describe('RPC connection setup', () => {
  test('connections page shows RPC configuration section', async ({ page }) => {
    await signIn(page)
    await page.getByRole('link', { name: /connections/i }).click()
    await expect(page.getByRole('heading', { name: /rpc/i })).toBeVisible({
      timeout: 3000,
    })
  })

  test('RPC form discloses data sharing to endpoint', async ({ page }) => {
    await signIn(page)
    await page.getByRole('link', { name: /connections/i }).click()
    // The page must explain what the endpoint sees
    await expect(page.getByText(/wallet address/i)).toBeVisible()
  })

  test('saving empty RPC URL shows validation error', async ({ page }) => {
    await signIn(page)
    await page.getByRole('link', { name: /connections/i }).click()
    await page.getByRole('button', { name: /save rpc/i }).click()
    await expect(page.getByRole('alert')).toContainText(/required/i)
  })

  test('private host option is available and labeled', async ({ page }) => {
    await signIn(page)
    await page.getByRole('link', { name: /connections/i }).click()
    // The private host checkbox must be clearly labeled
    const checkbox = page.getByRole('checkbox', { name: /private.*host/i })
    await expect(checkbox).toBeVisible()
    await expect(checkbox).not.toBeChecked()
  })

  test('RPC page shows Ethereum-only context', async ({ page }) => {
    await signIn(page)
    await page.getByRole('link', { name: /connections/i }).click()
    // Must not suggest multi-chain support that does not exist
    await expect(page.getByText(/ethereum/i)).toBeVisible()
  })

  test('test connection button is shown when RPC is configured', async ({ page }) => {
    await signIn(page)
    // Configure a (potentially invalid) RPC first
    await page.getByRole('link', { name: /connections/i }).click()
    await page.getByLabel('RPC URL').fill('https://mainnet.example-rpc.invalid')
    await page.getByRole('button', { name: /save rpc/i }).click()

    // After saving, a test-connection button should appear
    await expect(
      page.getByRole('button', { name: /test connection/i }),
    ).toBeVisible({ timeout: 5000 })
  })
})

test.describe('Public address addition', () => {
  test('wallets page shows address addition form', async ({ page }) => {
    await signIn(page)
    await page.getByRole('link', { name: /wallets/i }).click()
    await expect(page.getByLabel('Ethereum address')).toBeVisible()
  })

  test('add wallet without proof of ownership wording', async ({ page }) => {
    await signIn(page)
    await page.getByRole('link', { name: /wallets/i }).click()
    // The page must not claim ownership — use "tracked address" wording
    const pageText = await page.textContent('main')
    expect(pageText?.toLowerCase()).not.toContain('your wallet')
    expect(pageText?.toLowerCase()).not.toContain('your address')
    // Should contain neutral tracked-address language
    expect(pageText?.toLowerCase()).toContain('tracked')
  })

  test('adding an invalid address shows an error', async ({ page }) => {
    await signIn(page)
    await page.getByRole('link', { name: /wallets/i }).click()
    await page.getByLabel('Ethereum address').fill('not-a-valid-address')
    await page.getByRole('button', { name: /add tracked address/i }).click()
    await expect(page.getByRole('alert')).toBeVisible({ timeout: 3000 })
  })

  test('adding a valid public Ethereum address succeeds', async ({ page }) => {
    await signIn(page)
    await page.getByRole('link', { name: /wallets/i }).click()
    // Vitalik's public address — no ownership claim implied
    const address = '0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045'
    await page.getByLabel('Ethereum address').fill(address)
    await page.getByRole('button', { name: /add tracked address/i }).click()
    // Address should appear in the list
    await expect(page.getByText(address.toLowerCase()).or(page.getByText(address))).toBeVisible({
      timeout: 5000,
    })
  })

  test('refresh balances and discover tokens are separate actions', async ({ page }) => {
    await signIn(page)
    await page.getByRole('link', { name: /wallets/i }).click()
    await expect(page.getByRole('button', { name: /refresh balances/i })).toBeVisible()
    await expect(page.getByRole('button', { name: /discover tokens/i })).toBeVisible()
  })

  test('discover tokens explains that refresh does not find new contracts', async ({
    page,
  }) => {
    await signIn(page)
    await page.getByRole('link', { name: /wallets/i }).click()
    await page.getByRole('button', { name: /discover tokens/i }).click()
    // The UI must clarify what refresh vs discover does
    await expect(
      page.getByText(/does not discover/i).or(page.getByText(/outside catalog/i)),
    ).toBeVisible({ timeout: 3000 })
  })
})
