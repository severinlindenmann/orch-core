// A settings change on a real host is a terminal hand-off (M3 machinery, unchanged): the page collects what the person
// wants, the host prepares the request, and the person signs in a terminal. The dashboard signs nothing.
import { useRef, useState, type ReactNode } from 'react'
import type { HandoffAction } from '@/api/handoff'
import { SignPopover } from '@/components/sign/SignPopover'

export interface HandoffSpec {
  /** Core's title: "Make Tom a maintainer". */
  title: string
  action: HandoffAction | null
  /** Prepare as the dialog opens (the page already holds the whole choice). Without it the dialog shows `form` first. */
  confirmLabel?: string
  hint?: string | null
  form?: ReactNode
  /** Plain lines shown while reviewing when core's review has nothing under its headline. */
  summary?: ReactNode
  /** The ONE toast after the signature (the outcome in plain words). Default: "<title>: signed". */
  doneToast?: string
  /** True while the signature is being made: the review folds into the button that was pressed, which says so. */
  onSigning?: (signing: boolean) => void
  /** The review ended: signed (true) or not (false: cancelled, closed, refused). */
  onEnd?: (signed: boolean) => void
  /** Show the review under the row (an accordion) instead of a popover. */
  inline?: boolean
  /** The person's own press (a button in a dialog that said what happens) opened this: the host's review opens at once.
   * Prepare only, never armed: Sign stays the person's own press. */
  prepareOnOpen?: boolean
  /** Lines kept in the review's main view above the host's review (e.g. where files go, by name). */
  lead?: ReactNode | ((content: import('@/api/signing').SignContent | undefined) => ReactNode)
}

export function useSettingsHandoff(ws: string | undefined, onSettled?: () => void | Promise<unknown>): { ask: (spec: HandoffSpec) => void; dialog: ReactNode; inline: boolean } {
  const [spec, setSpec] = useState<(HandoffSpec & { n: number }) | null>(null)
  const signed = useRef(false)
  const dialog = spec && (
    <SignPopover
      key={spec.n}
      title={spec.title}
      ws={ws}
      action={spec.action}
      prepareOnOpen={spec.prepareOnOpen}
      autoStart={spec.prepareOnOpen ? false : undefined}
      confirmLabel={spec.confirmLabel}
      hint={spec.hint}
      summary={spec.summary}
      lead={spec.lead}
      inline={spec.inline}
      doneToast={spec.doneToast}
      onSigning={spec.onSigning}
      onSettled={(phase) => {
        if (phase !== 'done') return
        signed.current = true
        // The page behind shows the new state as soon as the host has said it; the caller's end hook runs after that read.
        void Promise.resolve(onSettled?.()).finally(() => spec.onEnd?.(true))
      }}
      onClose={() => {
        if (!signed.current) spec.onEnd?.(false)
        signed.current = false
        setSpec(null)
      }}
    >
      {spec.form}
    </SignPopover>
  )
  return { ask: (s) => setSpec((old) => ({ ...s, n: (old?.n ?? 0) + 1 })), dialog, inline: !!spec?.inline }
}
