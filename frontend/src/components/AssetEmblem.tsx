import { useState } from 'react'

interface Props {
  symbol: string
  /** Optional remote logo. Falls back to the generated monogram when absent or broken. */
  logoUrl?: string | null
  size?: 'md' | 'sm'
}

/**
 * Emblem palette — hue pairs used to build the monogram gradient. Index is derived
 * deterministically from the symbol so an asset keeps the same colour across renders,
 * pages and sessions without any server-side state.
 */
const EMBLEM_HUES: Array<[number, number]> = [
  [222, 262], [152, 182], [12, 32], [38, 58], [272, 302],
  [192, 212], [332, 352], [102, 132], [252, 282], [172, 202],
]

/** Stable, order-independent hash of the symbol — FNV-1a, truncated to 32 bits. */
function symbolHash(symbol: string): number {
  let h = 0x811c9dc5
  for (let i = 0; i < symbol.length; i += 1) {
    h ^= symbol.charCodeAt(i)
    h = Math.imul(h, 0x01000193) >>> 0
  }
  return h
}

/** Up to three alphanumeric characters — enough to tell assets apart inside the circle. */
export function monogram(symbol: string): string {
  const cleaned = symbol.replace(/[^A-Za-z0-9]/g, '').toUpperCase()
  return cleaned.slice(0, 3) || '?'
}

export default function AssetEmblem({ symbol, logoUrl, size = 'md' }: Props) {
  const [logoBroken, setLogoBroken] = useState(false)

  const className = `asset-emblem${size === 'sm' ? ' asset-emblem-sm' : ''}`

  if (logoUrl && !logoBroken) {
    return (
      <img
        className={className}
        src={logoUrl}
        alt=""
        aria-hidden="true"
        loading="lazy"
        onError={() => setLogoBroken(true)}
      />
    )
  }

  const text = monogram(symbol)
  const [h1, h2] = EMBLEM_HUES[symbolHash(symbol) % EMBLEM_HUES.length]

  return (
    <span
      className={`${className} asset-emblem-monogram asset-emblem-len-${text.length}`}
      aria-hidden="true"
      style={{
        background: `linear-gradient(135deg, hsl(${h1} 62% 46%), hsl(${h2} 58% 38%))`,
      }}
    >
      {text}
    </span>
  )
}
