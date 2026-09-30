/**
 * Hash-based routing for the main nav tabs (AUD-350).
 *
 * The active tab used to live in React state only, so every page reload — and
 * every browser back/forward — dropped the user back on the dashboard. Keeping
 * the tab in `location.hash` (e.g. `#/wallets`) makes a reload restore the tab
 * the user was on, makes back/forward walk the tab history, and makes tabs
 * linkable. The hash is used rather than a path so the SPA needs no server-side
 * rewrite beyond the one nginx already does for `/`.
 */
import { useCallback, useEffect, useState } from 'react'
import { MAIN_PAGES } from './components/Layout'
import type { MainPage } from './components/Layout'

const DEFAULT_PAGE: MainPage = 'dashboard'

function isMainPage(value: string): value is MainPage {
  return (MAIN_PAGES as readonly string[]).includes(value)
}

/** Parse `#/wallets` (or the legacy `#wallets`) into a known page. */
export function pageFromHash(hash: string): MainPage | null {
  const slug = hash.replace(/^#\/?/, '').trim()
  return isMainPage(slug) ? slug : null
}

export function hashForPage(page: MainPage): string {
  return `#/${page}`
}

/**
 * Read the active tab from the URL hash and keep the two in sync.
 *
 * An unknown or empty hash resolves to the dashboard, and the hash is
 * normalised on first render so the URL always names the visible tab.
 */
export function useHashPage(): [MainPage, (page: MainPage) => void] {
  const [page, setPageState] = useState<MainPage>(
    () => pageFromHash(window.location.hash) ?? DEFAULT_PAGE,
  )

  // Follow browser back/forward and any external hash change.
  useEffect(() => {
    function onHashChange() {
      setPageState(pageFromHash(window.location.hash) ?? DEFAULT_PAGE)
    }
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  // Normalise the URL whenever it does not name a valid tab (first load, a bad
  // link), using replaceState so it does not add a bogus history entry. After a
  // setPage the hash already matches, so this is a no-op.
  useEffect(() => {
    if (pageFromHash(window.location.hash) === null) {
      window.history.replaceState(null, '', hashForPage(page))
    }
  }, [page])

  const setPage = useCallback((next: MainPage) => {
    setPageState(next)
    if (pageFromHash(window.location.hash) !== next) {
      window.location.hash = hashForPage(next)
    }
  }, [])

  return [page, setPage]
}
