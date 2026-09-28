import { test, expect, type Page } from '@playwright/test'

/**
 * E2E browser journeys for US4 operational pages:
 * - SchedulesPage (T089): schedule/budget settings with cost warnings
 * - StatusPage (T090): job/worker/cooldown/recovery status and cancellation
 * - AccountDataPage (T091): password change, data export (JSON/CSV), purge preview/confirm
 *
 * Tests run at 390px (mobile) and 1440px (desktop) viewports.
 * Require a running backend with an authenticated session and the frontend dev server.
 */

const APP_URL = process.env.APP_URL ?? 'http://localhost:5173'
const OWNER_PASSWORD = process.env.OWNER_PASSWORD ?? 'correct-horse-battery-staple-42'

async function signIn(page: Page): Promise<void> {
  await page.goto(APP_URL)
  const heading = page.getByRole('heading')
  const text = await heading.textContent()
  if (text && /set up/i.test(text)) {
    await page.getByLabel('Password').first().fill(OWNER_PASSWORD)
    const confirm = page.getByLabel('Confirm password')
    if (await confirm.isVisible()) {
      await confirm.fill(OWNER_PASSWORD)
    }
    await page.getByRole('button', { name: /set up/i }).click()
    await page.waitForSelector('[role="navigation"]', { timeout: 5000 })
  } else if (text && /sign in/i.test(text)) {
    await page.getByLabel('Password').fill(OWNER_PASSWORD)
    await page.getByRole('button', { name: /sign in/i }).click()
    await page.waitForSelector('[role="navigation"]', { timeout: 5000 })
  }
}

async function navigateTo(page: Page, label: string): Promise<void> {
  await page.getByRole('button', { name: label }).click()
}

const VIEWPORTS = [
  { name: '390px (mobile)', width: 390, height: 844 },
  { name: '1440px (desktop)', width: 1440, height: 900 },
] as const

for (const viewport of VIEWPORTS) {
  test.describe(`SchedulesPage — ${viewport.name}`, () => {
    test.use({ viewport: { width: viewport.width, height: viewport.height } })

    test('shows Schedules page with balance, discovery, and quote schedule fields', async ({
      page,
    }) => {
      await signIn(page)
      await navigateTo(page, 'Schedules')
      await expect(page.getByRole('heading', { name: /schedules/i })).toBeVisible()
      await expect(page.getByRole('group', { name: /balance/i }).or(
        page.locator('fieldset').filter({ hasText: /balance/i }),
      )).toBeVisible()
      await expect(
        page.locator('fieldset').filter({ hasText: /discovery/i }),
      ).toBeVisible()
      await expect(
        page.locator('fieldset').filter({ hasText: /quote/i }),
      ).toBeVisible()
    })

    test('shows projected usage section', async ({ page }) => {
      await signIn(page)
      await navigateTo(page, 'Schedules')
      await expect(
        page.getByRole('region', { name: /projected usage/i }).or(
          page.getByText(/projected usage/i),
        ),
      ).toBeVisible()
    })

    test('shows cost versus freshness warning text', async ({ page }) => {
      await signIn(page)
      await navigateTo(page, 'Schedules')
      await expect(page.getByText(/cost versus freshness/i)).toBeVisible()
    })

    test('save button is disabled when no changes made', async ({ page }) => {
      await signIn(page)
      await navigateTo(page, 'Schedules')
      const saveBtn = page.getByRole('button', { name: /save schedule/i })
      await expect(saveBtn).toBeDisabled()
    })

    test('all interactive elements are keyboard navigable', async ({ page }) => {
      await signIn(page)
      await navigateTo(page, 'Schedules')
      const checkboxes = page.getByRole('checkbox')
      const count = await checkboxes.count()
      expect(count).toBeGreaterThan(0)
      for (let i = 0; i < count; i++) {
        await expect(checkboxes.nth(i)).toBeFocusable()
      }
      const inputs = page.getByRole('spinbutton')
      const inputCount = await inputs.count()
      for (let i = 0; i < inputCount; i++) {
        await expect(inputs.nth(i)).toBeFocusable()
      }
    })

    test('shows cost warning when interval is set very low for quotes', async ({
      page,
    }) => {
      await signIn(page)
      await navigateTo(page, 'Schedules')

      const quotesFieldset = page.locator('fieldset').filter({ hasText: /quote/i })
      const intervalInput = quotesFieldset.locator('input[type="number"]')
      if (await intervalInput.isVisible()) {
        await intervalInput.fill('60')
        await intervalInput.dispatchEvent('change')
        await expect(
          quotesFieldset.locator('[role="note"]').or(
            page.getByText(/requests\/day/i),
          ),
        ).toBeVisible({ timeout: 2000 })
      }
    })
  })

  test.describe(`StatusPage — ${viewport.name}`, () => {
    test.use({ viewport: { width: viewport.width, height: viewport.height } })

    test('shows Status page with system status section', async ({ page }) => {
      await signIn(page)
      await navigateTo(page, 'Status')
      await expect(page.getByRole('heading', { name: /^status$/i })).toBeVisible()
      await expect(
        page.getByRole('region', { name: /system status/i }).or(
          page.getByText(/system status/i),
        ),
      ).toBeVisible()
    })

    test('shows database status', async ({ page }) => {
      await signIn(page)
      await navigateTo(page, 'Status')
      await expect(page.getByText(/database/i)).toBeVisible()
    })

    test('shows worker heartbeat field', async ({ page }) => {
      await signIn(page)
      await navigateTo(page, 'Status')
      await expect(page.getByText(/worker heartbeat/i)).toBeVisible()
    })

    test('shows next execution fields for schedules', async ({ page }) => {
      await signIn(page)
      await navigateTo(page, 'Status')
      await expect(page.getByText(/next execution/i).first()).toBeVisible()
    })

    test('shows last attempt field for jobs', async ({ page }) => {
      await signIn(page)
      await navigateTo(page, 'Status')
      // Job history section should have last attempt visible or say no jobs
      const jobsSection = page.getByRole('region', { name: /job history/i })
      await expect(jobsSection).toBeVisible()
      const hasJobs = await page.getByText(/last attempt/i).isVisible()
      const noJobs = await page.getByText(/no jobs have run/i).isVisible()
      expect(hasJobs || noJobs).toBe(true)
    })

    test('shows manual trigger buttons', async ({ page }) => {
      await signIn(page)
      await navigateTo(page, 'Status')
      await expect(
        page.getByRole('button', { name: /trigger balance scan/i }),
      ).toBeVisible()
      await expect(
        page.getByRole('button', { name: /trigger.*discovery/i }),
      ).toBeVisible()
      await expect(
        page.getByRole('button', { name: /trigger.*quote/i }),
      ).toBeVisible()
    })

    test('trigger buttons are keyboard accessible', async ({ page }) => {
      await signIn(page)
      await navigateTo(page, 'Status')
      const btn = page.getByRole('button', { name: /trigger balance scan/i })
      await expect(btn).toBeFocusable()
    })

    test('running jobs show a cancel button', async ({ page }) => {
      await signIn(page)
      await navigateTo(page, 'Status')
      const cancelBtns = page.getByRole('button', { name: /cancel.*job/i })
      const count = await cancelBtns.count()
      // If there are running jobs, cancel button is visible. Otherwise absent is fine.
      if (count > 0) {
        await expect(cancelBtns.first()).toBeVisible()
        await expect(cancelBtns.first()).toBeFocusable()
      }
    })
  })

  test.describe(`AccountDataPage — ${viewport.name}`, () => {
    test.use({ viewport: { width: viewport.width, height: viewport.height } })

    test('shows Account & Data page with three main sections', async ({ page }) => {
      await signIn(page)
      await navigateTo(page, 'Account & Data')
      await expect(page.getByRole('heading', { name: /account.*data/i })).toBeVisible()
      await expect(
        page.getByRole('region', { name: /change password/i }).or(
          page.getByText(/change password/i),
        ),
      ).toBeVisible()
      await expect(
        page.getByRole('region', { name: /export data/i }).or(
          page.getByText(/export data/i),
        ),
      ).toBeVisible()
      await expect(
        page.getByRole('region', { name: /provider data purge/i }).or(
          page.getByText(/provider data purge/i),
        ),
      ).toBeVisible()
    })

    test.describe('Password change', () => {
      test('change password form has all required fields', async ({ page }) => {
        await signIn(page)
        await navigateTo(page, 'Account & Data')
        await expect(page.getByLabel('Current password')).toBeVisible()
        await expect(page.getByLabel('New password')).toBeVisible()
        await expect(page.getByLabel('Confirm new password')).toBeVisible()
        await expect(
          page.getByRole('button', { name: /change password/i }),
        ).toBeVisible()
      })

      test('rejects new password shorter than 12 characters', async ({ page }) => {
        await signIn(page)
        await navigateTo(page, 'Account & Data')
        await page.getByLabel('Current password').fill(OWNER_PASSWORD)
        await page.getByLabel('New password').fill('short')
        await page.getByLabel('Confirm new password').fill('short')
        await page.getByRole('button', { name: /change password/i }).click()
        await expect(page.getByRole('alert')).toContainText(/at least 12/i)
      })

      test('rejects mismatched passwords', async ({ page }) => {
        await signIn(page)
        await navigateTo(page, 'Account & Data')
        await page.getByLabel('Current password').fill(OWNER_PASSWORD)
        await page.getByLabel('New password').fill('new-valid-password-1234')
        await page.getByLabel('Confirm new password').fill('different-password-1234')
        await page.getByRole('button', { name: /change password/i }).click()
        await expect(page.getByRole('alert')).toContainText(/do not match/i)
      })

      test('all password fields are keyboard accessible', async ({ page }) => {
        await signIn(page)
        await navigateTo(page, 'Account & Data')
        await expect(page.getByLabel('Current password')).toBeFocusable()
        await expect(page.getByLabel('New password')).toBeFocusable()
        await expect(page.getByLabel('Confirm new password')).toBeFocusable()
      })
    })

    test.describe('Export', () => {
      test('export section shows portfolio and history options', async ({ page }) => {
        await signIn(page)
        await navigateTo(page, 'Account & Data')
        await expect(
          page.getByRole('radio', { name: /current portfolio/i }),
        ).toBeVisible()
        await expect(
          page.getByRole('radio', { name: /full history/i }),
        ).toBeVisible()
      })

      test('export section shows JSON and CSV format options', async ({ page }) => {
        await signIn(page)
        await navigateTo(page, 'Account & Data')
        await expect(page.getByRole('radio', { name: /json/i })).toBeVisible()
        await expect(page.getByRole('radio', { name: /csv/i })).toBeVisible()
      })

      test('export button is visible and keyboard accessible', async ({ page }) => {
        await signIn(page)
        await navigateTo(page, 'Account & Data')
        const exportBtn = page.getByRole('button', { name: /export.*portfolio.*json/i })
        await expect(exportBtn).toBeVisible()
        await expect(exportBtn).toBeFocusable()
      })

      test('selecting history format changes export button label', async ({ page }) => {
        await signIn(page)
        await navigateTo(page, 'Account & Data')
        await page.getByRole('radio', { name: /full history/i }).click()
        await expect(
          page.getByRole('button', { name: /export.*history/i }),
        ).toBeVisible()
      })

      test('selecting CSV changes export button label', async ({ page }) => {
        await signIn(page)
        await navigateTo(page, 'Account & Data')
        await page.getByRole('radio', { name: /csv/i }).click()
        await expect(
          page.getByRole('button', { name: /export.*csv/i }),
        ).toBeVisible()
      })
    })

    test.describe('Purge', () => {
      test('purge section shows provider input and preview button', async ({ page }) => {
        await signIn(page)
        await navigateTo(page, 'Account & Data')
        await expect(page.getByLabel('Provider')).toBeVisible()
        await expect(
          page.getByRole('button', { name: /preview impact/i }),
        ).toBeVisible()
      })

      test('purge confirm button is disabled before preview and confirmation', async ({
        page,
      }) => {
        await signIn(page)
        await navigateTo(page, 'Account & Data')
        const confirmBtn = page.getByRole('button', {
          name: /confirm and purge/i,
        })
        // Should not be visible before preview is loaded
        await expect(confirmBtn).not.toBeVisible()
      })

      test('all purge interactive elements are keyboard accessible', async ({
        page,
      }) => {
        await signIn(page)
        await navigateTo(page, 'Account & Data')
        await expect(page.getByLabel('Provider')).toBeFocusable()
        await expect(
          page.getByRole('button', { name: /preview impact/i }),
        ).toBeFocusable()
      })
    })
  })
}
