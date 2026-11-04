// The parts of the simpler sign dialog that sit around the signed content (variant B): the agent access terms, "What happens next", and the footer lines (how you sign, the quiet phone link). The content itself is
// still the host's review, shown in full; nothing here replaces it and nothing here is signed.
import { AGENT_MAY_SHORT, type SignStory } from './story'

export function WhatNext({ story }: { story: SignStory }) {
  if (!story.steps.length) return null
  return (
    <section data-testid="what-next" aria-label="What happens next" className="space-y-1 text-[13px]">
      <h3 className="font-medium text-text">What happens next</h3>
      <ol className="list-decimal space-y-0.5 pl-5 text-text-muted">{story.steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
    </section>
  )
}

/** The agent access terms, when this signature gives it. Nothing is typed: the review above and Sign are the confirmation. */
export function AgentAccess({ story }: { story: SignStory | null }) {
  if (!story?.access) return null
  return (
    <section data-testid="agent-access" aria-label="Agent access" className="rounded-md border border-border p-3 text-[13px]">
      <p data-testid="access-length">Agents (Claude Code) may work for you for {story.access.length}. {AGENT_MAY_SHORT}</p>
    </section>
  )
}

export function SignMethodLine({ text, onPhone, phoneDisabled }: { text: string; onPhone?: () => void; phoneDisabled?: boolean }) {
  return (
    <div className="space-y-1 text-[13px]">
      <p data-testid="sign-method" className="text-text-muted">{text}</p>
      {onPhone && <button type="button" data-testid="use-phone" disabled={phoneDisabled} onClick={onPhone} className="min-h-11 text-text-muted underline underline-offset-4 disabled:opacity-50">Use my phone instead</button>}
    </div>
  )
}
