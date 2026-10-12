// A settings change on a real host is a terminal hand-off (M3 machinery, unchanged): the page collects what the person
// wants, the host prepares the request, and the person signs in a terminal. The dashboard signs nothing.
import { useState, type ReactNode } from 'react'
import type { HandoffAction } from '@/api/handoff'
import { HandoffDialog } from '@/components/sign/HandoffDialog'

export interface HandoffSpec {
  /** Core's title: "Make Tom a maintainer". */
  title: string
  action: HandoffAction | null
  /** Prepare as the dialog opens (the page already holds the whole choice). Without it the dialog shows `form` first. */
  autoStart?: boolean
  confirmLabel?: string
  hint?: string | null
  form?: ReactNode
}

export function useSettingsHandoff(ws: string | undefined, onSettled?: () => void): { ask: (spec: HandoffSpec) => void; dialog: ReactNode } {
  const [spec, setSpec] = useState<(HandoffSpec & { n: number }) | null>(null)
  const dialog = spec && (
    <HandoffDialog
      key={spec.n}
      title={spec.title}
      ws={ws}
      action={spec.action}
      autoStart={spec.autoStart}
      confirmLabel={spec.confirmLabel}
      hint={spec.hint}
      onSettled={(phase) => phase === 'done' && onSettled?.()}
      onClose={() => setSpec(null)}
    >
      {spec.form}
    </HandoffDialog>
  )
  return { ask: (s) => setSpec((old) => ({ ...s, n: (old?.n ?? 0) + 1 })), dialog }
}
