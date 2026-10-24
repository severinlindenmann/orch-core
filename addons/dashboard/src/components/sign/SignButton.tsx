// The button of a signature (run 8, V2): it carries the outcome ("Sign and start task"), and after the press the review
// folds into this same button, which keeps its width and says what is happening: "Signing…", then (when the caller says
// an agent is starting) "Starting agent…", and "Still starting…" when that takes longer than 8 s.
import { Loader2 } from 'lucide-react'
import { useEffect, useRef, useState, type ComponentProps } from 'react'
import { Button } from '@/components/ui/button'

export const SLOW_AFTER_MS = 8000

/** What the button says while it waits: null when nothing is pending. */
export function pendingLabel(p: { signing: boolean; after?: string | null; slow: boolean; slowLabel?: string }): string | null {
  if (p.signing) return 'Signing…'
  if (p.after) return p.slow ? (p.slowLabel ?? 'Still working…') : p.after
  return null
}

/** True once `on` has been true for `ms`. */
export function useSlow(on: boolean, ms = SLOW_AFTER_MS): boolean {
  const [slow, setSlow] = useState(false)
  useEffect(() => {
    setSlow(false)
    if (!on) return
    const t = setTimeout(() => setSlow(true), ms)
    return () => clearTimeout(t)
  }, [on, ms])
  return slow
}

export interface SignButtonProps extends Omit<ComponentProps<typeof Button>, 'children'> {
  /** The idle label: the outcome ("Sign and start task"). */
  label: string
  /** True while the signature is being made (from SignPopover's onSigning). */
  signing?: boolean
  /** After the signature: what the app is doing now ("Starting agent…"). Null when nothing. */
  after?: string | null
  /** What `after` turns into when it takes longer than 8 s ("Still starting…"). */
  slowLabel?: string
}

export function SignButton({ label, signing = false, after = null, slowLabel, disabled, className, ...rest }: SignButtonProps) {
  const slow = useSlow(!!after && !signing)
  const text = pendingLabel({ signing, after, slow, slowLabel })
  const ref = useRef<HTMLButtonElement>(null)
  const [width, setWidth] = useState<number | null>(null)
  useEffect(() => {
    if (!text && ref.current) setWidth(ref.current.getBoundingClientRect().width)
  }, [text, label])
  useEffect(() => {
    const el = ref.current
    if (el && text && width) el.style.minWidth = `${width}px`
    else if (el) el.style.minWidth = ''
  }, [text, width])
  return (
    <Button ref={ref} type="button" {...rest} className={className} disabled={disabled || !!text} aria-busy={text ? true : undefined} data-sign-button data-pending={text ? '' : undefined}>
      {text ? (
        <>
          <Loader2 className="size-4 animate-spin" aria-hidden /> {text}
        </>
      ) : (
        label
      )}
    </Button>
  )
}
