// The frame of every signature review (run 8, V2): a popover anchored to the button that was pressed on a wide screen, a
// bottom sheet on a phone, and a centred dialog where no button is known (a menu item, the command palette). The content
// is the same in all three; this file only decides where it sits.
import { ShieldCheck } from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import { Popover as PopoverPrimitive } from 'radix-ui'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'

/** The last button, link or menu item that was pressed: the review of a signature opens next to it. */
let last: Element | null = null
if (typeof document !== 'undefined') {
  const note = (e: Event) => {
    const t = e.target instanceof Element ? e.target.closest('button, [role="button"], [role="menuitem"], a') : null
    if (t) last = t
  }
  document.addEventListener('pointerdown', note, true)
  document.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') note(e) }, true)
}

/** The element a review that opens right now belongs to; null when it is gone (a closed menu) or not a button. */
export function lastTrigger(): HTMLElement | null {
  return last instanceof HTMLElement && last.isConnected && last.getClientRects().length > 0 ? last : null
}

const WIDE = '(min-width: 640px)'
function useWide(): boolean {
  const [wide, setWide] = useState(() => typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia(WIDE).matches)
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const m = window.matchMedia(WIDE)
    const on = () => setWide(m.matches)
    m.addEventListener('change', on)
    return () => m.removeEventListener('change', on)
  }, [])
  return wide
}

export interface SignShellProps {
  anchor: HTMLElement | null
  /** False folds the frame away (the signature is being made and the button says so); the component above stays mounted. */
  open: boolean
  onDismiss: () => void
  title: ReactNode
  /** Under the title; may be a workspace line and a sentence. */
  description: ReactNode
  showClose: boolean
  onOpenAutoFocus: (e: Event) => void
  children: ReactNode
}

/** The row with Cancel and the sign button: it stays at the bottom of the scrolling review, so Cancel is always reachable (phone sheet and popover). */
export const STICKY_FOOTER = 'sticky bottom-0 z-10 -mx-4 -mb-4 border-t border-border bg-surface px-4 pb-4 pt-3'

const BODY = 'grid max-h-[85dvh] grid-cols-[minmax(0,1fr)] gap-3 overflow-y-auto'

export function SignShell(p: SignShellProps) {
  const wide = useWide()
  const heading = (
    <div className="flex items-center gap-2 text-[15px] font-semibold leading-snug">
      <ShieldCheck className="size-4 shrink-0 text-brand" aria-hidden />
      <span className="min-w-0">{p.title}</span>
    </div>
  )
  if (wide && p.anchor) {
    const anchor = p.anchor
    return (
      <PopoverPrimitive.Root open={p.open} onOpenChange={(o) => !o && p.onDismiss()} modal>
        <PopoverPrimitive.Anchor virtualRef={{ current: anchor }} />
        <PopoverPrimitive.Portal>
          <PopoverPrimitive.Content
            data-handoff-dialog
            data-sign-popover
            side="bottom"
            align="start"
            sideOffset={8}
            collisionPadding={12}
            onOpenAutoFocus={p.onOpenAutoFocus}
            aria-label={typeof p.title === 'string' ? p.title : undefined}
            className="z-50 w-[min(32rem,calc(100vw-1.5rem))] rounded-lg border border-border bg-surface p-0 text-text shadow-xl outline-none data-[state=open]:animate-in data-[state=open]:fade-in-0"
          >
            <div className={cn(BODY, 'p-4 max-h-[min(80dvh,var(--radix-popover-content-available-height))]')}>
              <header className="space-y-1">
                {heading}
                <div className="text-[13px] text-text-muted">{p.description}</div>
              </header>
              {p.children}
            </div>
          </PopoverPrimitive.Content>
        </PopoverPrimitive.Portal>
      </PopoverPrimitive.Root>
    )
  }
  return (
    <Dialog open={p.open} onOpenChange={(o) => !o && p.onDismiss()}>
      <DialogContent
        showCloseButton={p.showClose}
        data-handoff-dialog
        data-sign-sheet
        className={cn(BODY, 'gap-4 border-border bg-surface p-4 max-sm:top-auto max-sm:bottom-0 max-sm:max-w-full max-sm:translate-y-0 max-sm:rounded-b-none sm:max-w-lg')}
        onOpenAutoFocus={p.onOpenAutoFocus}
      >
        <header className="space-y-1">
          <DialogTitle className="flex items-center gap-2">
            <ShieldCheck className="size-4 text-brand" aria-hidden />
            {p.title}
          </DialogTitle>
          <DialogDescription asChild>
            <div className="text-[13px] text-text-muted">{p.description}</div>
          </DialogDescription>
        </header>
        {p.children}
      </DialogContent>
    </Dialog>
  )
}
