import { useEffect, useId, useRef, useState } from 'react'

interface Props {
  /** Short description of what the notices are about, for screen readers. */
  label: string
  /** One entry per notice; rendered inside the popover. */
  notices: React.ReactNode[]
}

// AUD-686: data-quality warnings used to sit as full-width banners above the
// chart and ate a row of vertical space on every visit, even though the
// explanation is only needed once. They now collapse into a warning triangle
// next to the section title that reveals the text on hover, focus or tap.
export default function NoticeBadge({ label, notices }: Props) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const popoverId = useId()

  // Tap-to-open on touch devices has no matching "mouse left" event, so close
  // on an outside tap and on Escape.
  useEffect(() => {
    if (!open) return
    const onPointerDown = (e: PointerEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  if (notices.length === 0) return null

  return (
    <div
      ref={wrapRef}
      className="notice-badge"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
    >
      <button
        type="button"
        className="notice-badge-trigger"
        aria-label={`${label}: ${notices.length} notice${notices.length === 1 ? '' : 's'}`}
        aria-expanded={open}
        aria-controls={open ? popoverId : undefined}
        onClick={() => setOpen((v) => !v)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
      >
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <path
            d="M12 3.5 22 20.5H2L12 3.5Z"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinejoin="round"
          />
          <path
            d="M12 10v4.5"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
          />
          <circle cx="12" cy="17.4" r="1" fill="currentColor" />
        </svg>
        {notices.length > 1 && (
          <span className="notice-badge-count" aria-hidden="true">
            {notices.length}
          </span>
        )}
      </button>
      {open && (
        <div id={popoverId} role="note" className="notice-popover notice-list">
          {notices.map((n, i) => (
            <p key={i} className="notice-item">
              {n}
            </p>
          ))}
        </div>
      )}
    </div>
  )
}
