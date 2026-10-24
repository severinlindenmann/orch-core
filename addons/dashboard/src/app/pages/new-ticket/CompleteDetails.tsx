// "Complete details": after a signed `new`, what the new event cannot carry (priority, size, labels, the parent epic,
// the written sections) is offered as further hand-offs, one signature each, in order. Offered, never forced: closing
// the dialog (Cancel) leaves the rest as it is. The list shown is exactly what the next request will ask the host to set;
// the signed preview inside the dialog still comes only from the host's material.
import { useState } from 'react'
import type { FollowUpAction } from '@/api/handoff'
import { sectionLabel, type SectionName } from '@/api/sections'
import { SignPopover } from '@/components/sign/SignPopover'

/** What a step will set, as short lines for the person (the values they typed, nothing from the host). */
export function stepLines(a: FollowUpAction): string[] {
  if (a.action === 'section') {
    const text = a.text.length > 160 ? `${a.text.slice(0, 160)}…` : a.text
    return [`${sectionLabel('feature', (a.section === 'findings' ? 'verification' : a.section) as SectionName)}: ${text}`]
  }
  return [
    ...(a.priority ? [`Priority: ${a.priority}`] : []),
    ...(a.size ? [`Size: ${a.size}`] : []),
    ...(a.labels?.length ? [`Labels: ${a.labels.join(', ')}`] : []),
    ...(a.parent ? [`Parent epic: ${a.parent}`] : []),
  ]
}

export function CompleteDetails({ ws, ticketKey, steps, onFinish, title }: { ws: string | undefined; ticketKey: string; steps: FollowUpAction[]; /** `done`: how many of `steps` were signed before it ended (all of them = finished, fewer = cancelled). */ onFinish: (done: number) => void; title?: string }) {
  const [index, setIndex] = useState(0)
  const step = steps[index]
  if (!step) return null
  const action = { ...step, key: ticketKey }
  const lines = stepLines(action)
  const last = index === steps.length - 1
  return (
    <SignPopover
      key={index}
      title={title ?? (steps.length > 1 ? `Complete details (${index + 1} of ${steps.length})` : 'Complete details')}
      ws={ws}
      action={action}
      confirmLabel={action.action === 'section' ? 'Prepare section' : 'Prepare details'}
      onSettled={(phase) => {
        if (phase === 'done') (last ? () => onFinish(steps.length) : () => setIndex(index + 1))()
      }}
      onClose={() => onFinish(index)}
    >
      <div className="space-y-2 text-[13px] text-text-muted">
        <p>
          {ticketKey} is created.{' '}
          {action.action === 'section' ? 'Its written text is one more signature.' : 'What the new-ticket event cannot carry is one more signature.'} Cancel to leave {steps.length > 1 ? 'the rest' : 'it'} as it is.
        </p>
        <p className="text-text">This will set:</p>
        <ul data-testid="complete-details-lines" className="list-disc space-y-0.5 pl-5 text-text">
          {lines.map((l) => (
            <li key={l} className="whitespace-pre-wrap break-words">
              {l}
            </li>
          ))}
        </ul>
      </div>
    </SignPopover>
  )
}
