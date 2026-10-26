import { cn } from '@/lib/utils'

/**
 * A small trend line (oldest value first), drawn in the brand colour. Decorative next to a number that already says
 * the value, so it carries a short text alternative ("from 3.10 to 5.40") and nothing interactive.
 */
export function Sparkline({ values, label, className }: { values: number[]; label: string; className?: string }) {
  const w = 72
  const h = 22
  // Only finite points; scaled by the largest magnitude first, so even extreme finite values never overflow to Infinity.
  const finite = values.filter(Number.isFinite)
  if (finite.length < 2) return null
  const scale = Math.max(...finite.map(Math.abs)) || 1
  const norm = finite.map((v) => v / scale)
  const min = Math.min(...norm)
  const span = Math.max(...norm) - min || 1
  const pts = norm.map((v, i) => `${((i / (norm.length - 1)) * (w - 2) + 1).toFixed(1)},${(h - 2 - ((v - min) / span) * (h - 4)).toFixed(1)}`)
  return (
    <svg role="img" aria-label={label} viewBox={`0 0 ${w} ${h}`} width={w} height={h} className={cn('shrink-0 text-brand', className)}>
      <polyline points={pts.join(' ')} fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={pts[pts.length - 1].split(',')[0]} cy={pts[pts.length - 1].split(',')[1]} r={2} fill="currentColor" />
    </svg>
  )
}
