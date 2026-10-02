/**
 * Hash-based routing for the main nav tabs (AUD-350) and per-screen view state
 * (AUD-353).
 *
 * The active tab lives in `location.hash` (e.g. `#/wallets`), and the query
 * part of the hash (e.g. `#/history?period=30d`) carries the view state a
 * screen's owner sets by hand — selected period, table filters, sort order,
 * view toggles. Keeping both in the URL means a reload or a back/forward
 * navigation restores exactly what the owner was looking at, and the screen
 * stays linkable. The hash is used rather than a path so the SPA needs no
 * per-route server-side rewrite — only `/` itself has to serve the shell,
 * which the API's static mount does (`backend/src/audr/api/spa.py`, AUD-388;
 * nginx did it before that).
 *
 * AUD-354 (deep links to a specific wallet/asset) is intentionally out of
 * scope here, but the split is chosen so it slots in without a rework: the
 * path segment names the entity, the query segment carries view settings.
 */
import { useCallback, useEffect, useState } from 'react'
import { MAIN_PAGES } from './components/Layout'
import type { MainPage } from './components/Layout'

const DEFAULT_PAGE: MainPage = 'dashboard'

function isMainPage(value: string): value is MainPage {
  return (MAIN_PAGES as readonly string[]).includes(value)
}

/** Split `#/wallets?foo=bar` into its page slug and raw query string. */
function splitHash(hash: string): { slug: string; search: string } {
  const body = hash.replace(/^#\/?/, '')
  const qIndex = body.indexOf('?')
  if (qIndex === -1) return { slug: body.trim(), search: '' }
  return { slug: body.slice(0, qIndex).trim(), search: body.slice(qIndex + 1) }
}

/** Parse `#/wallets` (or `#/wallets?foo=bar`, or the legacy `#wallets`) into a known page. */
export function pageFromHash(hash: string): MainPage | null {
  const { slug } = splitHash(hash)
  return isMainPage(slug) ? slug : null
}

export function hashForPage(page: MainPage, search?: string): string {
  return `#/${page}${search ? `?${search}` : ''}`
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

// ---- Per-screen view state (query part of the hash) ----

/**
 * Converts one view-state field to and from its URL-query string form.
 *
 * `serialize` returns `null` for the default value so the URL only ever
 * names the settings that differ from the default — a fresh screen has no
 * query string at all.
 */
export interface FieldCodec<T> {
  parse(raw: string | null): T
  serialize(value: T): string | null
}

export function stringField(defaultValue: string): FieldCodec<string> {
  return {
    parse: (raw) => raw ?? defaultValue,
    serialize: (value) => (value === defaultValue ? null : value),
  }
}

/** A field restricted to a fixed set of values; anything else normalises to the default. */
export function enumField<T extends string>(
  allowed: readonly T[],
  defaultValue: T,
): FieldCodec<T> {
  return {
    parse: (raw) => (raw !== null && (allowed as readonly string[]).includes(raw) ? (raw as T) : defaultValue),
    serialize: (value) => (value === defaultValue ? null : value),
  }
}

export function boolField(defaultValue: boolean): FieldCodec<boolean> {
  return {
    parse: (raw) => (raw === null ? defaultValue : raw === '1'),
    serialize: (value) => (value === defaultValue ? null : value ? '1' : '0'),
  }
}

/** A non-negative integer field (e.g. a pagination offset); anything invalid normalises to the default. */
export function intField(defaultValue: number): FieldCodec<number> {
  return {
    parse: (raw) => {
      if (raw === null) return defaultValue
      const n = Number.parseInt(raw, 10)
      return Number.isFinite(n) && n >= 0 ? n : defaultValue
    },
    serialize: (value) => (value === defaultValue ? null : String(value)),
  }
}

export type QuerySchema = Record<string, FieldCodec<unknown>>
export type QueryState<S extends QuerySchema> = { [K in keyof S]: ReturnType<S[K]['parse']> }

/** Parse a hash's raw query string (without the leading `?`) against a schema. */
export function parseQuery<S extends QuerySchema>(search: string, schema: S): QueryState<S> {
  const params = new URLSearchParams(search)
  const out = {} as QueryState<S>
  for (const key of Object.keys(schema)) {
    out[key as keyof S] = schema[key].parse(params.get(key)) as QueryState<S>[keyof S]
  }
  return out
}

/** Serialize view state back to a query string, omitting fields at their default value. */
export function serializeQuery<S extends QuerySchema>(state: QueryState<S>, schema: S): string {
  const params = new URLSearchParams()
  for (const key of Object.keys(schema)) {
    const serialized = schema[key].serialize(state[key as keyof S])
    if (serialized !== null) params.set(key, serialized)
  }
  return params.toString()
}

/**
 * Keep a screen's view state (period, filters, sort, toggles) in the query
 * part of its hash entry, e.g. `#/history?period=30d`.
 *
 * `schema` must be a stable (module-level) object — it is not tracked as a
 * dependency. Updates use `history.replaceState` rather than pushing a new
 * entry per keystroke/toggle; the tab-switching history entries already
 * created by `useHashPage` are what back/forward walk through, each carrying
 * whatever view state was last set for that tab.
 */
export function useHashQueryState<S extends QuerySchema>(
  page: MainPage,
  schema: S,
): [QueryState<S>, (patch: Partial<QueryState<S>>) => void] {
  const read = useCallback((): QueryState<S> => {
    const { search } = splitHash(window.location.hash)
    return parseQuery(search, schema)
  }, [])

  const [state, setState] = useState<QueryState<S>>(read)

  useEffect(() => {
    function onHashChange() {
      setState(read())
    }
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [read])

  const update = useCallback(
    (patch: Partial<QueryState<S>>) => {
      const { search } = splitHash(window.location.hash)
      const next = { ...parseQuery(search, schema), ...patch }
      setState(next)
      const nextHash = hashForPage(page, serializeQuery(next, schema))
      if (window.location.hash !== nextHash) {
        window.history.replaceState(null, '', nextHash)
      }
    },
    [page, schema],
  )

  return [state, update]
}
