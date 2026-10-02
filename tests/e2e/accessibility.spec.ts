import { test, expect, type Page } from '@playwright/test'

/**
 * Accessibility checks shared across the four owner journeys — auth,
 * valuation, history, operations (AUD-107 T094 / AUD-373).
 *
 * Four concerns, each exercised across all four journeys where applicable:
 *   1. Keyboard navigation — every control reachable and operable without a mouse.
 *   2. Text alternatives for charts — the allocation and history charts must expose
 *      an accessible data table, and must never render a fabricated chart when
 *      there is no priced data.
 *   3. English-only content — no other-script text leaks into the UI.
 *   4. Layouts at 390px (mobile) and 1440px (desktop).
 *
 * Requires a running backend with an empty (un-setup) database and the
 * frontend dev server, with the /api proxy wired to the backend — same
 * precondition as the other tests/e2e specs. Valuation and history data are
 * mocked via page.route so the chart assertions do not depend on real wallet
 * scans or quote providers.
 */

const APP_URL = process.env.APP_URL ?? 'http://localhost:5173'
const OWNER_PASSWORD = process.env.OWNER_PASSWORD ?? 'correct-horse-battery-staple-42'

// Cyrillic, Greek, Hebrew, Arabic, Hiragana/Katakana, CJK, Hangul — any of
// these appearing in visible UI text means the "English-only" rule (frontend
// Development Rule 5) has been violated. Ordinary English typography (em
// dashes, ellipses, curly quotes, currency symbols) is outside these ranges.
const FOREIGN_SCRIPT =
  /[Ͱ-ϿЀ-ӿ֐-׿؀-ۿ぀-ヿ㐀-鿿가-힣]/

function assertEnglishOnly(text: string | null, where: string): void {
  expect(text, `${where} must contain only English text`).not.toMatch(FOREIGN_SCRIPT)
}

async function ensureSignedIn(page: Page): Promise<void> {
  await page.goto(APP_URL)
  const heading = page.getByRole('heading').first()
  await heading.waitFor()
  const text = (await heading.textContent()) ?? ''
  if (/set up/i.test(text)) {
    await page.getByLabel('Password', { exact: true }).fill(OWNER_PASSWORD)
    await page.getByLabel('Confirm password').fill(OWNER_PASSWORD)
    await page.getByRole('button', { name: /set up/i }).click()
    await page.waitForSelector('[role="navigation"]', { timeout: 5000 })
  } else if (/sign in/i.test(text)) {
    await page.getByLabel('Password', { exact: true }).fill(OWNER_PASSWORD)
    await page.getByRole('button', { name: /sign in/i }).click()
    await page.waitForSelector('[role="navigation"]', { timeout: 5000 })
  }
}

async function goToPage(page: Page, label: string): Promise<void> {
  await page.getByRole('button', { name: label, exact: true }).click()
}

const MOCK_ALLOCATION_PORTFOLIO = {
  snapshot_id: 'snap-a11y-1',
  membership_revision: '1',
  valuation_time: '2026-01-15T00:00:00Z',
  currency: 'USD',
  priced_subtotal_usd: null,
  total_usd: '12345.67',
  quality: {
    incomplete: false,
    stale_balances: false,
    stale_prices: false,
    mixed_observation_times: false,
    discovery_overdue: false,
    verification_pending: false,
    invalidated: false,
  },
  balance_block: 1,
  balance_block_time: '2026-01-15T00:00:00Z',
  balance_observed_at: '2026-01-15T00:00:00Z',
  discovery_completed_at: '2026-01-15T00:00:00Z',
  holdings: [
    {
      wallet_id: 'w-a11y-1',
      asset_id: 'asset-a11y-eth',
      contract_address: null,
      is_native: true,
      raw_balance: '1000000000000000000',
      decimals: 18,
      quantity: '1',
      price_usd: '12345.67',
      value_usd: '12345.67',
      included: true,
      metadata_source: 'catalog',
      read_status: 'ok',
      block_time: '2026-01-15T00:00:00Z',
      observed_at: '2026-01-15T00:00:00Z',
      last_success_at: '2026-01-15T00:00:00Z',
    },
  ],
  allocations: [
    { asset_id: 'asset-a11y-eth', symbol: 'ETH', value_usd: '12345.67', percentage: '100.00' },
  ],
  stale_contribution_usd: null,
  request_id: 'req-a11y-1',
  generated_at: '2026-01-15T00:00:00Z',
}

const MOCK_EMPTY_PORTFOLIO = {
  ...MOCK_ALLOCATION_PORTFOLIO,
  total_usd: null,
  holdings: [],
  allocations: [],
}

const MOCK_HISTORY_ENTRIES = [
  {
    snapshot_id: 'hist-a11y-1',
    snapshotted_at: '2026-01-13T00:00:00Z',
    total_value_usd: '11000.00',
    quality: 'ok' as const,
    included_wallet_count: 1,
    included_asset_count: 1,
    has_gap: false,
    is_canonical: true,
    is_gap_marker: false,
  },
  {
    snapshot_id: 'hist-a11y-2',
    snapshotted_at: '2026-01-14T00:00:00Z',
    total_value_usd: '11800.00',
    quality: 'ok' as const,
    included_wallet_count: 1,
    included_asset_count: 1,
    has_gap: false,
    is_canonical: true,
    is_gap_marker: false,
  },
  {
    snapshot_id: 'hist-a11y-3',
    snapshotted_at: '2026-01-15T00:00:00Z',
    total_value_usd: '12345.67',
    quality: 'ok' as const,
    included_wallet_count: 1,
    included_asset_count: 1,
    has_gap: false,
    is_canonical: true,
    is_gap_marker: false,
  },
]

async function mockHistory(page: Page, entries: typeof MOCK_HISTORY_ENTRIES): Promise<void> {
  await page.route('**/api/v1/history*', async (route) => {
    const period = new URL(route.request().url()).searchParams.get('period') ?? '30d'
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ period, entries, next_cursor: null }),
    })
  })
}

async function mockPortfolio(
  page: Page,
  portfolio: typeof MOCK_ALLOCATION_PORTFOLIO,
): Promise<void> {
  await page.route('**/api/v1/portfolio*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(portfolio),
    })
  })
  await page.route('**/api/v1/events*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ total: 0, limit: 7, offset: 0, events: [] }),
    })
  })
  await page.route('**/api/v1/assets/*/news*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ asset_id: 'asset-a11y-eth', total: 0, limit: 5, offset: 0, news: [] }),
    })
  })
}

// ---------------------------------------------------------------------------
// 1. Keyboard navigation — no mouse required
// ---------------------------------------------------------------------------

test.describe('Keyboard navigation — all journeys', () => {
  test('auth journey: Tab order reaches the submit button without a mouse', async ({ page }) => {
    await page.goto(APP_URL)
    const heading = page.getByRole('heading').first()
    await heading.waitFor()
    const isSetup = /set up/i.test((await heading.textContent()) ?? '')

    await page.keyboard.press('Tab')
    await expect(page.getByLabel('Password', { exact: true })).toBeFocused()

    if (isSetup) {
      await page.keyboard.press('Tab')
      await expect(page.getByLabel('Confirm password')).toBeFocused()
    }

    await page.keyboard.press('Tab')
    await expect(
      page.getByRole('button', { name: isSetup ? /set up/i : /sign in/i }),
    ).toBeFocused()
  })

  test('valuation journey: sidebar nav and history range switcher are keyboard operable', async ({
    page,
  }) => {
    await mockPortfolio(page, MOCK_ALLOCATION_PORTFOLIO)
    await mockHistory(page, MOCK_HISTORY_ENTRIES)
    await ensureSignedIn(page)

    const historyNav = page.getByRole('button', { name: 'History', exact: true })
    await historyNav.focus()
    await expect(historyNav).toBeFocused()
    await page.keyboard.press('Enter')
    await expect(page.getByRole('heading', { name: /^history$/i })).toBeVisible()

    const dashboardNav = page.getByRole('button', { name: 'Dashboard', exact: true })
    await dashboardNav.focus()
    await page.keyboard.press('Enter')
    await expect(page.getByRole('heading', { name: /overview/i })).toBeVisible()

    const weekButton = page
      .getByRole('group', { name: /history range/i })
      .getByRole('button', { name: '1W' })
    await weekButton.focus()
    await expect(weekButton).toBeFocused()
    await expect(weekButton).toHaveAttribute('aria-pressed', 'true')
  })

  test('history journey: range tabs are reachable and operable by keyboard', async ({ page }) => {
    await mockHistory(page, MOCK_HISTORY_ENTRIES)
    await ensureSignedIn(page)

    const historyNav = page.getByRole('button', { name: 'History', exact: true })
    await historyNav.focus()
    await page.keyboard.press('Enter')
    await expect(page.getByRole('heading', { name: /^history$/i })).toBeVisible()

    const rangeGroup = page.getByRole('group', { name: /range/i })
    const dayTab = rangeGroup.getByRole('button', { name: '24h', exact: true })
    await dayTab.focus()
    await expect(dayTab).toBeFocused()
    await page.keyboard.press('Enter')
    await expect(dayTab).toHaveAttribute('aria-pressed', 'true')
  })

  test('operations journey: schedule controls are reachable and operable by keyboard', async ({
    page,
  }) => {
    await ensureSignedIn(page)
    await goToPage(page, 'Schedules')
    await expect(page.getByRole('heading', { name: /schedules/i })).toBeVisible()

    const balancesToggle = page.getByLabel(/enable balance scans/i)
    await balancesToggle.focus()
    await expect(balancesToggle).toBeFocused()
    await page.keyboard.press('Space')
    await expect(balancesToggle).not.toBeChecked()

    await page.keyboard.press('Space')
    await expect(balancesToggle).toBeChecked()

    const intervalInput = page.getByLabel(/interval — currently/i).first()
    await intervalInput.focus()
    await expect(intervalInput).toBeFocused()
  })
})

// ---------------------------------------------------------------------------
// 2. Text alternatives for charts
// ---------------------------------------------------------------------------

test.describe('Text alternatives for charts', () => {
  test('valuation: allocation pie chart has an accessible data table alternative', async ({
    page,
  }) => {
    await mockPortfolio(page, MOCK_ALLOCATION_PORTFOLIO)
    await mockHistory(page, MOCK_HISTORY_ENTRIES)
    await ensureSignedIn(page)

    await expect(page.getByRole('img', { name: /asset allocation pie chart/i })).toBeVisible()
    const table = page.getByRole('table', { name: /asset allocation/i })
    await expect(table).toBeVisible()
    await expect(table.getByRole('cell', { name: 'ETH' })).toBeVisible()
  })

  test('valuation: no priced holdings → explanatory note, never a fabricated chart', async ({
    page,
  }) => {
    await mockPortfolio(page, MOCK_EMPTY_PORTFOLIO)
    await mockHistory(page, [])
    await ensureSignedIn(page)

    await expect(page.getByRole('img', { name: /asset allocation pie chart/i })).not.toBeVisible()
    await expect(page.getByText(/no holdings found/i)).toBeVisible()
  })

  test('history: portfolio value chart has an accessible data table alternative', async ({
    page,
  }) => {
    await mockHistory(page, MOCK_HISTORY_ENTRIES)
    await ensureSignedIn(page)
    await goToPage(page, 'History')

    const chart = page.getByRole('img', { name: /portfolio value history chart/i })
    await expect(chart).toBeVisible()

    const summary = page.locator('details summary')
    await summary.focus()
    await page.keyboard.press('Enter')
    const table = page.getByRole('table', { name: /portfolio value history/i })
    await expect(table).toBeVisible()
    await expect(table.getByRole('columnheader', { name: /total \(usd\)/i })).toBeVisible()
  })

  test('history: no data renders an explanatory empty state, never a blank chart', async ({
    page,
  }) => {
    await mockHistory(page, [])
    await ensureSignedIn(page)
    await goToPage(page, 'History')

    await expect(page.getByRole('img', { name: /portfolio value history chart/i })).not.toBeVisible()
    await expect(page.getByText(/no history data available/i)).toBeVisible()
  })
})

// ---------------------------------------------------------------------------
// 3. English-only content
// ---------------------------------------------------------------------------

test.describe('English-only content — all journeys', () => {
  test('auth journey entry screen is English-only', async ({ page }) => {
    await page.goto(APP_URL)
    await page.getByRole('heading').first().waitFor()
    assertEnglishOnly(await page.locator('body').innerText(), 'the auth entry screen')
  })

  test('valuation journey (Dashboard) is English-only', async ({ page }) => {
    await mockPortfolio(page, MOCK_ALLOCATION_PORTFOLIO)
    await mockHistory(page, MOCK_HISTORY_ENTRIES)
    await ensureSignedIn(page)
    assertEnglishOnly(await page.locator('main').innerText(), 'the Dashboard page')
  })

  test('history journey page is English-only', async ({ page }) => {
    await mockHistory(page, MOCK_HISTORY_ENTRIES)
    await ensureSignedIn(page)
    await goToPage(page, 'History')
    assertEnglishOnly(await page.locator('main').innerText(), 'the History page')
  })

  test('operations journey pages (Schedules, Status, Account & Data) are English-only', async ({
    page,
  }) => {
    await ensureSignedIn(page)

    await goToPage(page, 'Schedules')
    assertEnglishOnly(await page.locator('main').innerText(), 'the Schedules page')

    await goToPage(page, 'Status')
    assertEnglishOnly(await page.locator('main').innerText(), 'the Status page')

    await goToPage(page, 'Account & Data')
    assertEnglishOnly(await page.locator('main').innerText(), 'the Account & Data page')
  })
})

// ---------------------------------------------------------------------------
// 4. Layouts at 390px and 1440px
// ---------------------------------------------------------------------------

const VIEWPORTS = [
  { name: '390px (mobile)', width: 390, height: 844 },
  { name: '1440px (desktop)', width: 1440, height: 900 },
] as const

for (const viewport of VIEWPORTS) {
  test.describe(`Layouts — ${viewport.name}`, () => {
    test.use({ viewport: { width: viewport.width, height: viewport.height } })

    test('auth journey entry screen renders its heading and submit button', async ({ page }) => {
      await page.goto(APP_URL)
      const heading = page.getByRole('heading').first()
      await expect(heading).toBeVisible()
      const isSetup = /set up/i.test((await heading.textContent()) ?? '')
      await expect(
        page.getByRole('button', { name: isSetup ? /set up/i : /sign in/i }),
      ).toBeVisible()
    })

    test('valuation journey (Dashboard) renders the total and allocation chart', async ({
      page,
    }) => {
      await mockPortfolio(page, MOCK_ALLOCATION_PORTFOLIO)
      await mockHistory(page, MOCK_HISTORY_ENTRIES)
      await ensureSignedIn(page)
      await expect(page.getByRole('heading', { name: /overview/i })).toBeVisible()
      await expect(page.getByRole('img', { name: /asset allocation pie chart/i })).toBeVisible()
    })

    test('history journey renders the chart or its empty state', async ({ page }) => {
      await mockHistory(page, MOCK_HISTORY_ENTRIES)
      await ensureSignedIn(page)
      await goToPage(page, 'History')
      await expect(page.getByRole('heading', { name: /^history$/i })).toBeVisible()
      await expect(page.getByRole('img', { name: /portfolio value history chart/i })).toBeVisible()
    })

    test('operations journey (Schedules) renders its form sections', async ({ page }) => {
      await ensureSignedIn(page)
      await goToPage(page, 'Schedules')
      await expect(page.getByRole('heading', { name: /schedules/i })).toBeVisible()
      await expect(
        page.getByRole('button', { name: /save schedule settings/i }),
      ).toBeVisible()
    })
  })
}
