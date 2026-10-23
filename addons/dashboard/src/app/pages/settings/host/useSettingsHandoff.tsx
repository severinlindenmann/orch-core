// A settings change on a real host is a terminal hand-off (M3 machinery, unchanged): the page collects what the person
// wants, the host prepares the request, and the person signs in a terminal. The dashboard signs nothing.
import { useState, type ReactNode } from 'react'
import type { HandoffAction } from '@/api/handoff'
import { SignPopover } from '@/components/sign/SignPopover'

export interface HandoffSpec {
  /** Core's title: "Make Tom a maintainer". */
  title: string
  action: HandoffAction | null
  /** Prepare as the dialog opens (the page already holds the whole choice). Without it the dialog shows `form` first. */
  autoStart?: boolean
  confirmLabel?: string
  hint?: string | null
  form?: ReactNode
  /** Plain lines shown while reviewing when core's review has nothing under its headline. */
  summary?: ReactNode
}

export function useSettingsHandoff(ws: string | undefined, onSettled?: () => void): { ask: (spec: HandoffSpec) => void; dialog: ReactNode } {
  const [spec, setSpec] = useState<(HandoffSpec & { n: number }) | null>(null)
  const dialog = spec && (
    <SignPopover
      key={spec.n}
      title={spec.title}
      ws={ws}
      action={spec.action}
      autoStart={spec.autoStart}
      confirmLabel={spec.confirmLabel}
      hint={spec.hint}
      summary={spec.summary}
      onSettled={(phase) => {
        if (phase !== 'done') return
        onSettled?.()
        // Signed: the dialog is finished and the page behind it shows the new state. A moment of "Signed" first.
        const n = spec.n
        setTimeout(() => setSpec((cur) => (cur && cur.n === n ? null : cur)), 1500)
      }}
      onClose={() => setSpec(null)}
    >
      {spec.form}
    </SignPopover>
  )
  return { ask: (s) => setSpec((old) => ({ ...s, n: (old?.n ?? 0) + 1 })), dialog }
}
