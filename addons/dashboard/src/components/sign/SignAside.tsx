// The parts of the simpler sign dialog that sit around the signed content (variant B): the agent access terms with the
// typed word, "What happens next", and the footer lines (how you sign, the quiet phone link). The content itself is
// still the host's review, shown in full; nothing here replaces it and nothing here is signed.
import { useId } from 'react'
import { Raw } from './visible'
import { AGENT_MAY, type SignStory } from './story'

export function WhatNext({ story }: { story: SignStory }) {
  if (!story.steps.length) return null
  return (
    <section data-testid="what-next" aria-label="What happens next" className="space-y-1 text-[13px]">
      <h3 className="font-medium text-text">What happens next</h3>
      <ol className="list-decimal space-y-0.5 pl-5 text-text-muted">{story.steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
    </section>
  )
}

/** The agent access (when this signature gives it) and the word to type before Sign turns on. */
export function TypedWord({ word, story, value, onChange, disabled }: { word: string; story: SignStory | null; value: string; onChange: (v: string) => void; disabled?: boolean }) {
  const id = useId()
  return (
    <section data-testid="typed-word" aria-label={story?.access ? 'Agent access' : 'Confirm'} className="space-y-2 rounded-md border border-border p-3 text-[13px]">
      {story?.access && (
        <>
          <h3 className="font-medium text-text">Agent access</h3>
          <p data-testid="access-length">Let agents work for you for {story.access.length}.</p>
          <p><strong className="font-medium text-text">What agents may do</strong><br />{AGENT_MAY}</p>
        </>
      )}
      <label htmlFor={id} className="block">Type <strong className="font-medium text-text"><Raw>{word}</Raw></strong> to confirm</label>
      <input id={id} data-typed-word type="text" value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)} autoComplete="off" autoCapitalize="none" spellCheck={false} className="h-10 w-full rounded-md border border-border bg-bg px-3 font-mono text-[14px] text-text" />
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
