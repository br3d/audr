/**
 * Brand logo elements.
 *
 * The artwork is single-colour line art, so the only thing that changes between
 * the dark and the light theme is the ink. Rather than reading the theme in
 * React — the auth screens render outside the theme provider — the swap is done
 * in CSS off the `data-theme` attribute on the document root, and these are thin
 * semantic wrappers around that. See `.brand-mark` / `.brand-lockup` in
 * `styles/folio.css` and `scripts/gen_brand_assets.py` for the asset pipeline.
 */

interface LogoProps {
  className?: string
  /** Accessible name; pass `null` when an adjacent text label already names it. */
  label?: string | null
}

/** The mountain-and-candles mark on its own, without the wordmark. */
export function BrandMark({ className, label = 'audr' }: LogoProps) {
  return (
    <span
      className={`brand-mark${className ? ` ${className}` : ''}`}
      role={label === null ? 'presentation' : 'img'}
      aria-label={label ?? undefined}
      aria-hidden={label === null ? true : undefined}
    />
  )
}

/** The full lockup: the mark stacked above the `audr` wordmark. */
export function BrandLockup({ className, label = 'audr' }: LogoProps) {
  return (
    <span
      className={`brand-lockup${className ? ` ${className}` : ''}`}
      role={label === null ? 'presentation' : 'img'}
      aria-label={label ?? undefined}
      aria-hidden={label === null ? true : undefined}
    />
  )
}
