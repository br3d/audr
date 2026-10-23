import { ApiError } from './client'

/** The server's own message when available, else a caller-supplied fallback. */
export function errorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback
}
