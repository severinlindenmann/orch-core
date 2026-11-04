import { cn } from '@/lib/utils'

/** The Orbit mark. The hex colours are the dark-theme look and the fallback; the classes switch the ring, dot and cut-out with the theme (#421). */
export function OrbitMark({ size = 24, className }: { size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      role="img"
      aria-label="orch"
      className={cn('shrink-0', className)}
    >
      <circle cx="32" cy="32" r="20" fill="none" stroke="#e8eaed" className="stroke-text" strokeWidth="6" />
      <circle cx="32" cy="32" r="6" fill="#2fc6a3" className="fill-brand" />
      <circle cx="47" cy="17" r="7" fill="#2fc6a3" stroke="#0a0c0f" className="fill-brand stroke-sidebar" strokeWidth="3" />
    </svg>
  )
}

/** Mark + wordmark: "orch" (Geist 700) with a small "core" in Geist Mono. */
export function Wordmark({ size = 24, className }: { size?: number; className?: string }) {
  return (
    <span className={cn('inline-flex items-center gap-2', className)}>
      <OrbitMark size={size} />
      <span className="inline-flex items-baseline gap-1 leading-none">
        <span className="font-bold tracking-[-0.02em] text-text" style={{ fontSize: size * 0.75 }}>
          orch
        </span>
        <span className="font-mono text-brand" style={{ fontSize: size * 0.45 }}>
          core
        </span>
      </span>
    </span>
  )
}
