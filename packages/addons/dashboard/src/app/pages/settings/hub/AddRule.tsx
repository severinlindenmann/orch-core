import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { api } from '@/api/client'
import type { HubPreview, HubRule } from '@/api/hub'
import { ApiError } from '@/api/types'
import { trustedPress } from '@/components/sign/trustedPress'
import { WorkspaceLine } from '@/components/sign/WorkspaceLine'
import { Raw } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { InfoDrawer } from '@/components/InfoDrawer'
import { caveat } from './shared'
import type { HubWorkspace } from './StopSharing'

// Sharing MORE is a plain click, no signature and no typed word: a click-only host op (owner, full local session, the
// plan of the preview this session was shown). The review's facts come from the host's preview (readers, widened items, counts, what leaves); the page adds only labels (the ticket title, the workspace name)
// (`review`): which hub, who can read there, only the NEW items, what leaves per item, and that later edits follow.
// The list of items stays behind one small click. A changed plan shows the review again; nothing is auto-confirmed.

function refusal(e: unknown): string {
  const code = e instanceof ApiError ? e.code : ''
  if (code === 'role.denied') return 'Only the workspace owner can share more.'
  if (code === 'session.readonly' || code === 'op.not_allowed' || code === 'step_up_required') return 'Sign in on this computer to share more.'
  if (code === 'host.busy') return 'Too many sharing changes. Wait a minute, then try again.'
  if (code === 'invalid.input') return 'What would be shared changed meanwhile. Close this and look again.'
  return 'Could not share. Try again.'
}

const SHOWN_NAMES = 5
const SHOWN_WIDENED = 3

/** "Ben Test, team finance and 4 more": the first names, then how many are left (never a silent cut; the whole list is behind the i). */
function firstNames(names: string[]): string {
  return names.slice(0, SHOWN_NAMES).join(', ') + (names.length > SHOWN_NAMES ? ` and ${names.length - SHOWN_NAMES} more` : '')
}

function readerNames(r: { people: string[]; teams: string[] }): string[] {
  return [...r.people, ...r.teams.map(t => 'team ' + t)]
}

/** True when the review adds nothing: no item newly leaves and nobody is reached more (the host lists readers only for those). */
const nothingNew = (r: { new_count: number; widened: unknown[] }) => r.new_count === 0 && r.widened.length === 0

/** "Who will see it: Ben Test, team finance" / "Only you can see it." / "Nobody new will see it.": names from the hub, not signed (the host words the list). */
function who(r: { people: string[]; teams: string[] }, none: boolean): string {
  const names = readerNames(r)
  return names.length ? 'Who will see it: ' + firstNames(names) : none ? 'Nobody new will see it.' : 'Only you can see it.'
}

/** The compact sentence's subject: the host's readers (they may include people from other matching rules, which is right). */
function whoWill(r: { people: string[]; teams: string[] }): string {
  return firstNames(readerNames(r))
}

/** The widened row without its "ticket KEY:" prefix, then who is added (first names, "and N more"). */
function widenedText(w: { line: string; people_added: string[]; teams_added: string[]; description_added: boolean }, all = false): string {
  const added = readerNames({ people: w.people_added, teams: w.teams_added })
  return w.line.replace(/^ticket [^:]+:\s*/, '') + (added.length ? ' · adds ' + (all ? added.join(', ') : firstNames(added)) : '') + (w.description_added ? ' · now with its description' : '')
}

/** "feature tickets", "tickets labelled x", "ticket DEMO-1": the rule in words (the value is drawn verbatim, never reworded). */
function ruleWords(rule: HubRule, n: number, title?: string): React.ReactNode {
  const tickets = n === 1 ? 'ticket' : 'tickets'
  if (rule.kind === 'type') return <><Raw>{rule.value}</Raw> {tickets}</>
  if (rule.kind === 'tag') return <>{tickets} labelled <Raw>{rule.value}</Raw></>
  if (rule.kind === 'ticket') return <>ticket <Raw>{rule.value}</Raw>{title ? <> · <bdi><Raw>{title}</Raw></bdi></> : null}</>
  return <Raw>{rule.kind}</Raw>
}

export function AddRuleReview({ ws, workspace, rule, after, onDone, title, compact }: { ws: string; workspace: HubWorkspace; rule: HubRule; after: HubPreview | undefined; /** The share went through (the message was shown): the caller closes the dialog. */ onDone?: () => void; /** The ticket's title when the rule names one ticket. */ title?: string; /** One ticket from the ticket page: one sentence, one caveat, one button; the counts and lists are behind Details. */ compact?: boolean }) {
  const qc = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [list, setList] = useState(false)
  const review = after?.review
  const cut = !!review && (review.widened.length > SHOWN_WIDENED || review.widened.some(w => w.people_added.length + w.teams_added.length > SHOWN_NAMES))
  if (!after) return <p role="status">Loading the preview…</p>
  if (!review) return <p data-testid="hub-add-unsupported">Could not read the review. Try again.</p>
  const go = (e: React.MouseEvent) => {
    if (!trustedPress(e.nativeEvent) || busy || !after.plan) return
    setBusy(true)
    setError('')
    api.addHubRule(ws, rule, after.plan).then(
      got => {
        const what = rule.kind === 'ticket' && title ? `“${title}”` : `${review.new_count} ${review.new_count === 1 ? 'item' : 'items'}`
        toast.success(`Shared ${what} with the hub.`, { description: got.synced ? undefined : 'Saved here. It is sent when the hub is back.' })
        onDone?.(); void qc.invalidateQueries({ queryKey: ['hub', ws] })
      },
      err => setError(refusal(err)),
    ).finally(() => setBusy(false))
  }
  const names = whoWill(review.readers)
  const details = <InfoDrawer testId="hub-add-details">{compact && <p>{review.readers.note ?? 'Names from the hub, not signed.'}</p>}{compact && review.readers.info && <p>{review.readers.info}</p>}{compact && readerNames(review.readers).length > SHOWN_NAMES && <p data-testid="hub-add-readers-all">Everyone who will see it: <Raw>{readerNames(review.readers).join(', ')}</Raw></p>}<p data-testid="hub-add-rule">Tickets: <Raw>{rule.kind}</Raw>{rule.value !== null && <> · <Raw>{rule.value}</Raw></>}{rule.value !== null && rule.description ? ' · with description' : ''}</p><WorkspaceLine workspace={{ id: workspace.id, name: workspace.name, prefix: workspace.prefix }} many testId="hub-review-workspace" /><p data-testid="hub-add-hub">Hub: <Raw>{review.hub_id ?? 'unknown'}</Raw></p>{cut && <div data-testid="hub-add-widened-all">{review.widened.map((w, i) => <p key={i}>Reaches more now: <Raw>{widenedText(w, true)}</Raw></p>)}</div>}{after.plan && <p>Plan: <Raw>{after.plan}</Raw></p>}<p data-testid="hub-add-new">{review.new_count} new {review.new_count === 1 ? 'item leaves' : 'items leave'} this computer.</p>{review.leaves.map((t, i) => <p key={i} data-testid="hub-add-leaves">What leaves: <Raw>{t}</Raw></p>)}{review.new_items.length > 0 && <>
      <button type="button" className="text-[13px] text-text-muted underline" onClick={() => setList(v => !v)}>{list ? 'Hide list' : 'Show list'}</button>
      {list && <ul className="space-y-1" data-testid="hub-add-list">{review.new_items.slice(0, 20).map((l, i) => <li key={i}><Raw>{l}</Raw></li>)}
        {review.new_count > 20 && <li>and {review.new_count - 20} more</li>}</ul>}
    </>}</InfoDrawer>
  const widened = <>
    {review.widened.slice(0, SHOWN_WIDENED).map((w, i) => <p key={i} data-testid="hub-add-widened" className="text-warning">Reaches more now: <Raw>{widenedText(w)}</Raw></p>)}
    {review.widened.length > SHOWN_WIDENED && <p data-testid="hub-add-widened-more" className="text-warning">and {review.widened.length - SHOWN_WIDENED} more {review.widened.length - SHOWN_WIDENED === 1 ? 'ticket reaches' : 'tickets reach'} more people</p>}
  </>
  if (compact) return <div className="space-y-2" data-testid="hub-add-review">
    <p data-testid="hub-add-sentence">{names
      ? <><span data-testid="hub-add-readers"><bdi>{names}</bdi></span> will see {review.leaves.join('; ') || 'this ticket'}.</>
      : <span data-testid="hub-add-readers">{nothingNew(review) ? 'Nobody new will see it.' : 'Only you will see it.'}</span>}{review.later_edits && names ? <> <span data-testid="hub-add-later">{review.later_edits}</span></> : null}</p>
    {widened}
    <p className="text-text-muted text-[13px]">{caveat}</p>
    <div className="flex items-center justify-between gap-2">
      {details}
      <Button data-testid="hub-add-go" size="sm" disabled={busy || !after.plan} onClick={go}>{busy ? 'Sharing…' : 'Share'}</Button>
    </div>
    {error && <p role="alert" className="text-danger">{error}</p>}
  </div>
  return <div className="space-y-2" data-testid="hub-add-review">
    <p data-testid="hub-add-sentence">Share {rule.kind === 'ticket' ? null : <>{review.new_count} </>}{ruleWords(rule, review.new_count, title)} from <Raw>{workspace.name}</Raw> with the hub{rule.value !== null && rule.description ? ', with their descriptions' : ''}.</p>
    <div className="flex items-center gap-1" data-testid="hub-add-readers">
      <p><Raw>{who(review.readers, nothingNew(review))}</Raw></p>
      {(readerNames(review.readers).length > 0 || !!review.readers.info) && <InfoDrawer testId="hub-add-readers-info"><p>{review.readers.note ?? 'From the hub, not signed.'}</p>{review.readers.info && <p>{review.readers.info}</p>}{readerNames(review.readers).length > SHOWN_NAMES && <p data-testid="hub-add-readers-all">Everyone who will see it: <Raw>{readerNames(review.readers).join(', ')}</Raw></p>}</InfoDrawer>}
    </div>
    {widened}
    {details}
    {review.later_edits && <p data-testid="hub-add-later">{review.later_edits}</p>}
    <p className="text-warning">{caveat}</p>
    <Button data-testid="hub-add-go" size="sm" disabled={busy || !after.plan} onClick={go}>{busy ? 'Sharing…' : 'Share'}</Button>
    {error && <p role="alert" className="text-danger">{error}</p>}
  </div>
}
