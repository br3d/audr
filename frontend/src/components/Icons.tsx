import type { SVGProps } from 'react'

type IconProps = SVGProps<SVGSVGElement>

const defaults: IconProps = {
  width: 18,
  height: 18,
  viewBox: '0 0 20 20',
  fill: 'currentColor',
  'aria-hidden': 'true',
}

export function IconGrid(props: IconProps) {
  return (
    <svg {...defaults} {...props}>
      <rect x="2" y="2" width="7" height="7" rx="1.5" />
      <rect x="11" y="2" width="7" height="7" rx="1.5" />
      <rect x="2" y="11" width="7" height="7" rx="1.5" />
      <rect x="11" y="11" width="7" height="7" rx="1.5" />
    </svg>
  )
}

export function IconLayers(props: IconProps) {
  return (
    <svg {...defaults} {...props}>
      <path d="M10 2 2 6.5l8 4.5 8-4.5L10 2z" />
      <path d="M2 10.5l8 4.5 8-4.5" fill="none" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  )
}

export function IconWallet(props: IconProps) {
  return (
    <svg {...defaults} {...props}>
      <path d="M2 6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v1H2V6z" />
      <path d="M2 9h16v5a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V9z" />
      <circle cx="14.5" cy="12" r="1.5" fill="var(--bg-card)" />
    </svg>
  )
}

export function IconCoins(props: IconProps) {
  return (
    <svg {...defaults} {...props}>
      <circle cx="10" cy="9" r="5" fill="none" stroke="currentColor" strokeWidth="1.6" />
      <ellipse cx="10" cy="9" rx="5" ry="1.6" />
      <path d="M5 13c0 1.1 2.24 2 5 2s5-.9 5-2" fill="none" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  )
}

export function IconBarChart(props: IconProps) {
  return (
    <svg {...defaults} {...props}>
      <rect x="2" y="12" width="4" height="6" rx="1" />
      <rect x="8" y="7" width="4" height="11" rx="1" />
      <rect x="14" y="4" width="4" height="14" rx="1" />
    </svg>
  )
}

export function IconClock(props: IconProps) {
  return (
    <svg {...defaults} {...props}>
      <circle cx="10" cy="10" r="8" fill="none" stroke="currentColor" strokeWidth="1.6" />
      <path d="M10 6v4l3 3" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  )
}

export function IconActivity(props: IconProps) {
  return (
    <svg {...defaults} {...props}>
      <polyline
        points="2,10 5,10 7,5 9,15 12,8 14,12 16,10 18,10"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export function IconPlug(props: IconProps) {
  return (
    <svg {...defaults} {...props}>
      <path d="M7 2v5M13 2v5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
      <rect x="4" y="7" width="12" height="5" rx="2" fill="none" stroke="currentColor" strokeWidth="1.6" />
      <path d="M10 12v4" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
      <circle cx="10" cy="17" r="1" />
    </svg>
  )
}

export function IconSettings(props: IconProps) {
  return (
    <svg {...defaults} {...props}>
      <circle cx="10" cy="10" r="3" fill="none" stroke="currentColor" strokeWidth="1.6" />
      <path
        d="M10 2a.9.9 0 0 0-.9.9v1.26a6.1 6.1 0 0 0-1.48.62L6.71 3.86a.9.9 0 0 0-1.27 0l-.58.58a.9.9 0 0 0 0 1.27l.9.9a6.1 6.1 0 0 0-.62 1.48H3.9a.9.9 0 0 0-.9.9v.82a.9.9 0 0 0 .9.9h1.26c.14.52.35 1.01.62 1.48l-.9.9a.9.9 0 0 0 0 1.27l.58.58a.9.9 0 0 0 1.27 0l.9-.9c.47.27.96.48 1.48.62v1.26a.9.9 0 0 0 .9.9h.82a.9.9 0 0 0 .9-.9v-1.26a6.1 6.1 0 0 0 1.48-.62l.9.9a.9.9 0 0 0 1.27 0l.58-.58a.9.9 0 0 0 0-1.27l-.9-.9c.27-.47.48-.96.62-1.48h1.26a.9.9 0 0 0 .9-.9v-.82a.9.9 0 0 0-.9-.9h-1.26a6.1 6.1 0 0 0-.62-1.48l.9-.9a.9.9 0 0 0 0-1.27l-.58-.58a.9.9 0 0 0-1.27 0l-.9.9a6.1 6.1 0 0 0-1.48-.62V2.9A.9.9 0 0 0 10.82 2H10z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
      />
    </svg>
  )
}

export function IconSearch(props: IconProps) {
  return (
    <svg {...defaults} {...props}>
      <circle cx="9" cy="9" r="6" fill="none" stroke="currentColor" strokeWidth="1.6" />
      <path d="M13 13l4 4" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  )
}

export function IconPlus(props: IconProps) {
  return (
    <svg {...defaults} {...props}>
      <path d="M10 3v14M3 10h14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  )
}

export function IconX(props: IconProps) {
  return (
    <svg {...defaults} {...props}>
      <path d="M4 4l12 12M16 4L4 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  )
}

export function IconSun(props: IconProps) {
  return (
    <svg {...defaults} {...props}>
      <circle cx="10" cy="10" r="4" fill="none" stroke="currentColor" strokeWidth="1.6" />
      <path
        d="M10 2v2M10 16v2M2 10h2M16 10h2M4.22 4.22l1.42 1.42M14.36 14.36l1.42 1.42M4.22 15.78l1.42-1.42M14.36 5.64l1.42-1.42"
        fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"
      />
    </svg>
  )
}

export function IconMoon(props: IconProps) {
  return (
    <svg {...defaults} {...props}>
      <path d="M17 12.5a7 7 0 1 1-9.5-9.5 5.5 5.5 0 0 0 9.5 9.5z" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

export function IconSignOut(props: IconProps) {
  return (
    <svg {...defaults} {...props}>
      <path d="M8 3H4a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h4" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
      <path d="M13 7l4 3-4 3M17 10H8" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

export function IconRefresh(props: IconProps) {
  return (
    <svg {...defaults} {...props}>
      <path d="M4 10a6 6 0 1 0 1.7-4.3" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
      <path d="M4 4v3h3" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}
