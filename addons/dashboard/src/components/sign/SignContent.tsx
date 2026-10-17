import { QueryClientContext } from '@tanstack/react-query'
import { useContext, type ReactNode } from 'react'
import type { Workspace } from '@/api/types'
import type { SignContent as Content } from '@/api/signing'
import { Raw } from './visible'

export function SignDetails({ children }: { children: ReactNode }) {
  return <details className="space-y-3 text-[12px] text-text-muted"><summary className="cursor-pointer">Technical details</summary>{children}</details>
}
// Keep host-flattened ⏎ markers verbatim. Literal newlines (older hosts and block
// text) stay inside the value, with a visible mark and indentation on each line.
function Value({ children, block = false }: { children: string; block?: boolean }) {
  const lines = children.split('\n')
  if (!block && lines.length === 1) return <Raw>{children}</Raw>
  return <span className="inline-block max-w-full align-top">
    {lines.map((line, i) => <span key={i} className="flex gap-2 pl-4">
      <span aria-hidden="true" className="text-text-muted">|</span>
      <Raw>{line}</Raw>
    </span>)}
  </span>
}
/** A person id as the host shows it: `p_181d953f…` (full or cut). */
const PERSON = /^(p_[0-9a-f]+)(…|\.\.\.)?$/

/** The members of every workspace the dashboard already loaded (no fetch, no provider needed in tests). */
export function useKnownMembers(): { person: string; name: string }[] {
  const client = useContext(QueryClientContext)
  const all = client?.getQueryData<Workspace[]>(['workspaces']) ?? []
  return all.flatMap(w => w.members.filter(m => !m.name_hidden).map(m => ({ person: m.person, name: m.name })))
}
/** The name for a person id, or null when it is not an id or no single known person matches. Never guesses between two. */
export function personName(value: string | null, members: { person: string; name: string }[]): string | null {
  const m = value ? PERSON.exec(value) : null
  if (!m) return null
  const hits = members.filter(x => (m[2] ? x.person.startsWith(m[1]) : x.person === m[1]))
  return hits.length === 1 && new Set(hits.map(h => h.name)).size === 1 ? hits[0].name : null
}
export function SignContent({ content }: { content: Content }) {
  const members = useKnownMembers()
  const shown = (v: string | null) => v === null ? null : personName(v, members) ?? v
  const named = (content.fields_changed ?? []).flatMap(f => [f.before, f.after]).filter((v): v is string => v !== null && personName(v, members) !== null)
  return <div className="max-h-[38vh] space-y-4 overflow-auto text-[13px] [overflow-wrap:anywhere]">
    {content.ticket && <div>
      <p><Raw>{content.ticket.key}</Raw> · …<Raw>{content.ticket.uid_tail}</Raw></p>
      {content.ticket.title && <Value>{content.ticket.title}</Value>}
    </div>}
    {content.sections?.map(s => <section key={s.id}><h3 className="mb-1 text-[12px] font-medium text-text-muted"><Raw>{s.title}</Raw></h3><Value block>{s.text}</Value></section>)}
    {!!content.acceptance?.length && <section><h3 className="text-text-muted">Acceptance criteria</h3><ul className="space-y-1">{content.acceptance.map(a => <li key={a.id}><span aria-label={a.checked ? 'Checked' : 'Unchecked'}>{a.checked ? '☑' : '☐'}</span> <Raw>{a.id}</Raw> · <Value>{a.text}</Value></li>)}</ul></section>}
    {!!content.tasks?.length && <section><h3 className="text-text-muted">Plan tasks</h3><ol className="list-decimal space-y-2 pl-5">{content.tasks.map(t => <li key={t.n} value={t.n}><Raw>{t.id}</Raw> · <Value>{t.title}</Value>{t.verify?.map((v,i) => <p key={i} className="font-mono text-[12px]">Verify: <Value>{v}</Value></p>)}</li>)}</ol></section>}
    {!!content.fields_changed?.length && <section><h3 className="text-text-muted">Changed fields</h3>{content.fields_changed.map(f => <p key={f.field}><Raw>{f.field}</Raw>: <Value>{shown(f.before) ?? '—'}</Value>{f.before_note && <span className="text-text-muted"> (<Raw>{f.before_note}</Raw>)</span>} → <Value>{shown(f.after) ?? '—'}</Value></p>)}</section>}
    {[...(content.notes ?? []), ...(Array.isArray(content.note) ? content.note : content.note ? [content.note] : [])].map((note, i) => <p key={i}><Value>{note}</Value></p>)}
    {(content.ticket?.key_note || named.length > 0) && <details className="text-[12px] text-text-muted">
      <summary className="cursor-pointer">Technical details: ids and keys</summary>
      {content.ticket?.key_note && <p><Value>{content.ticket.key_note}</Value></p>}
      {[...new Set(named)].map(id => <p key={id}><Raw>{personName(id, members)!}</Raw> = <Raw>{id}</Raw></p>)}
    </details>}
  </div>
}
