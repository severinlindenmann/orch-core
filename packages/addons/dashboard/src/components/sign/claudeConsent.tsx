// P03: the one preselected line of a Claude Code start's sign card. The workspace's consent to open each ticket folder
// without Claude Code's own "trust this folder" question is recorded here, AFTER the person's own signature settled and
// before the agent starts (POST claude-trust, owner on a local session only: the host refuses anyone else). Never with
// "trust anyway" (allow_parent is false). Where the host cannot offer it, one plain reason replaces the line.
import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { queries } from '@/api/queries'
import { Checkbox } from '@/components/ui/checkbox'

/** The person is an owner of the workspace (a workspace without a roster, as a partial answer may be, is nobody's). */
export const isOwner = (w: { members?: { person: string; role: string }[] } | undefined, person: string | undefined) =>
  !!person && !!w?.members?.some((m) => m.person === person && m.role === 'owner')

export const CONSENT_LINE = "Let Claude Code open this ticket's folder without asking"
export const CONSENT_WHY = 'Recommended. Otherwise Claude Code waits for an answer and the agent cannot start. Change it any time in Settings.'

export interface ClaudeConsent {
  /** The host offers the consent and it is not given yet. */
  asks: boolean
  /** The host cannot give it: the plain reason ("" when there is none or it is given already). */
  refused: string
  ticked: boolean
  setTicked: (on: boolean) => void
  /** Records the consent once when asked and ticked; null when there is nothing to do. Never rejects. */
  grant: () => Promise<void> | null
  /** The line is on screen: only then may the choice be recorded (fail closed on any quiet or instant path). */
  markShown: () => void
  /** The host has not answered yet (an owner on a local session is asked). */
  pending: boolean
}

export function useClaudeConsent(ws: string | undefined, active: boolean): ClaudeConsent {
  const qc = useQueryClient()
  const q = useQuery({ ...queries.claudeTrust(ws ?? ''), enabled: active && !!ws })
  const [ticked, setTicked] = useState(true)
  const sent = useRef(false)
  const shown = useRef(false)
  const t = q.data
  const on = active && !!ws && !!t?.available
  const asks = on && t?.agent_needs_consent === true
  const refused = on && t?.agent_needs_consent === false ? (t.agent_why_not ?? '') : ''
  const grant = () => {
    if (!asks || !shown.current || !ticked || sent.current || !ws) return null
    sent.current = true
    return api.grantClaudeTrust(ws, false).then(
      () => { void qc.invalidateQueries({ queryKey: ['claude-trust', ws] }) },
      (e: unknown) => { toast.message(`Claude Code may still ask about the folder. ${e instanceof Error ? e.message : ''}`.trim()) },
    )
  }
  return { asks, refused, ticked, setTicked, grant, markShown: () => { shown.current = true }, pending: active && !!ws && q.isPending && q.fetchStatus !== 'idle' }
}

export function ClaudeConsentLine({ c }: { c: ClaudeConsent }) {
  const { markShown } = c
  useEffect(() => { if (c.asks) markShown() }, [c.asks, markShown])
  if (c.refused) return <p data-testid="claude-consent-refused" className="text-xs leading-5 text-text-muted">{c.refused}</p>
  if (!c.asks) return null
  return (
    <label className="flex items-start gap-2.5 text-xs leading-5" data-testid="claude-consent">
      <Checkbox className="mt-0.5" checked={c.ticked} onCheckedChange={(v) => c.setTicked(v === true)} aria-label={CONSENT_LINE} />
      <span><span className="font-medium text-text">{CONSENT_LINE}</span><br /><span className="text-text-muted">{CONSENT_WHY}</span></span>
    </label>
  )
}
