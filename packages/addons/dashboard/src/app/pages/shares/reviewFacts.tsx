import { useLayoutEffect, useRef, type ReactNode } from 'react'
import type { SignContent } from '@/api/signing'

/** Only summary lines and risk are outside the host's closed exact-value fold.
 * Match affirmative host sentences, never the title or folded fields/notes.
 */
export function reviewFacts(content: SignContent | undefined, days?: number) {
  const lines = [...(content?.summary?.lines ?? []), ...(content?.summary?.risk ? [content.summary.risk] : [])]
  return {
    destination: lines.some(line => /^(?:The files|Files) go to [^;\n]+(?:; the link opens (?:there too|at .+)\.)?$/.test(line)),
    publicAccess: lines.some(line => /^Anyone (?:who has|with) the link can (?:open|read) (?:the files|it)(?:[.,]|$)/.test(line)),
    duration: days !== undefined && lines.some(line => line.includes(`for ${days} day${days === 1 ? '' : 's'}.`)),
    linkOnce: lines.some(line => /^(?:You see|You will see) the link once[.;]/.test(line)),
  }
}

/** Keep the preparing lead's space when host facts replace its lines. */
export function ReservedLead({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => {
    const el = ref.current
    if (el) el.style.minHeight = `${Math.max(parseFloat(el.style.minHeight) || 0, el.getBoundingClientRect().height)}px`
  })
  return <div ref={ref} data-testid="reserved-share-lead">{children}</div>
}
