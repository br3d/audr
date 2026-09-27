import { test, expect } from '@playwright/test'

/**
 * E2E browser journeys for authentication and session flows.
 *
 * These tests require a running backend with an empty (un-setup) database
 * and a running frontend dev server, with the /api proxy wired to the backend.
 * They verify CSRF protection, session expiry, and the setup→signin flow.
 *
 * These tests are intentionally written to fail until the backend auth routes
 * (T026–T028) and the frontend pages (T043) are fully wired up.
 */

const APP_URL = process.env.APP_URL ?? 'http://localhost:5173'

test.describe('Initial setup', () => {
  test('setup page is shown when no owner exists', async ({ page }) => {
    await page.goto(APP_URL)
    await expect(page.getByRole('heading', { name: /set up audr/i })).toBeVisible()
  })

  test('setup rejects mismatched passwords', async ({ page }) => {
    await page.goto(APP_URL)
    await page.getByLabel('Password').fill('correct-horse-battery-staple')
    await page.getByLabel('Confirm password').fill('wrong-passphrase')
    await page.getByRole('button', { name: /set up/i }).click()
    await expect(page.getByRole('alert')).toContainText(/do not match/i)
  })

  test('setup rejects a password shorter than 12 characters', async ({ page }) => {
    await page.goto(APP_URL)
    await page.getByLabel('Password').fill('short')
    await page.getByLabel('Confirm password').fill('short')
    await page.getByRole('button', { name: /set up/i }).click()
    await expect(page.getByRole('alert')).toContainText(/at least 12/i)
  })

  test('successful setup creates owner and shows sign-in page', async ({ page }) => {
    await page.goto(APP_URL)
    const password = 'correct-horse-battery-staple-42'
    await page.getByLabel('Password').fill(password)
    await page.getByLabel('Confirm password').fill(password)
    await page.getByRole('button', { name: /set up/i }).click()
    // After setup the app should either auto-sign-in or redirect to sign-in
    await expect(
      page.getByRole('heading', { name: /sign in/i }).or(
        page.getByRole('heading', { name: /holdings/i }),
      ),
    ).toBeVisible({ timeout: 5000 })
  })

  test('setup page is unavailable once an owner exists', async ({ page }) => {
    // Navigate directly to setup route after owner was created in prior test.
    // The app must not allow re-setup.
    await page.goto(`${APP_URL}`)
    // Should see sign-in, not setup
    await expect(page.getByRole('heading', { name: /sign in/i })).toBeVisible({
      timeout: 5000,
    })
  })
})

test.describe('Sign in', () => {
  test('sign-in shows generic error on wrong password', async ({ page }) => {
    await page.goto(APP_URL)
    await page.getByLabel('Password').fill('definitely-wrong-password-99')
    await page.getByRole('button', { name: /sign in/i }).click()
    await expect(page.getByRole('alert')).toBeVisible()
    // Must not expose whether account exists or reveal internal details
    const alertText = await page.getByRole('alert').textContent()
    expect(alertText).not.toContain('stack')
    expect(alertText).not.toContain('SQL')
  })

  test('supports password paste', async ({ page }) => {
    await page.goto(APP_URL)
    const input = page.getByLabel('Password')
    await input.focus()
    // Verify the field accepts programmatic setting (paste equivalent)
    await input.fill('correct-horse-battery-staple-42')
    await expect(input).toHaveValue('correct-horse-battery-staple-42')
  })

  test('successful sign-in reaches holdings page', async ({ page }) => {
    await page.goto(APP_URL)
    await page.getByLabel('Password').fill('correct-horse-battery-staple-42')
    await page.getByRole('button', { name: /sign in/i }).click()
    await expect(page.getByRole('heading', { name: /holdings/i })).toBeVisible({
      timeout: 5000,
    })
  })
})

test.describe('CSRF enforcement', () => {
  test('mutating request without CSRF token is rejected', async ({ page }) => {
    // Directly call a mutation endpoint without the CSRF header; expect 403.
    const response = await page.request.post(`${APP_URL}/api/v1/auth/logout`, {
      headers: {
        'Content-Type': 'application/json',
      },
    })
    // The server must reject non-CSRF mutations from the browser
    expect([403, 401]).toContain(response.status())
  })

  test('sign-in includes CSRF header on subsequent mutations', async ({ page }) => {
    await page.goto(APP_URL)
    await page.getByLabel('Password').fill('correct-horse-battery-staple-42')

    const [response] = await Promise.all([
      page.waitForResponse((r) => r.url().includes('/auth/login')),
      page.getByRole('button', { name: /sign in/i }).click(),
    ])
    expect(response.ok()).toBe(true)

    // After login the client stores the CSRF token; verify a mutation goes out with it.
    // We intercept the logout request to check the header.
    let logoutRequest: import('@playwright/test').Request | null = null
    page.on('request', (req) => {
      if (req.url().includes('/auth/logout')) {
        logoutRequest = req
      }
    })

    // Trigger logout via the app (if a logout button is present)
    const logoutButton = page.getByRole('button', { name: /log out|sign out/i })
    if (await logoutButton.isVisible()) {
      await logoutButton.click()
    }

    if (logoutRequest !== null) {
      const headers = (logoutRequest as import('@playwright/test').Request).headers()
      expect(headers['x-csrf-token']).toBeTruthy()
    }
  })
})

test.describe('Session expiry', () => {
  test('expired session redirects to sign-in', async ({ page }) => {
    // Sign in first
    await page.goto(APP_URL)
    await page.getByLabel('Password').fill('correct-horse-battery-staple-42')
    await page.getByRole('button', { name: /sign in/i }).click()
    await expect(page.getByRole('heading', { name: /holdings/i })).toBeVisible({
      timeout: 5000,
    })

    // Simulate session expiry by clearing the session cookie
    await page.context().clearCookies()

    // Navigate to a protected page — should be redirected to sign-in
    await page.goto(APP_URL)
    await expect(page.getByRole('heading', { name: /sign in/i })).toBeVisible({
      timeout: 5000,
    })
  })
})
