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
/** A person id as the host writes it: `p_181d953f…` (cut) or the full `p_<hex>`. */
const PERSON = /^p_[0-9a-f]+(…|\.\.\.)?$/
const shortId = (id: string) => (id.length > 6 ? `${id.slice(0, 6)}…` : id)

/** The members of every workspace the dashboard already loaded (no fetch, no provider needed in tests). */
export function useKnownMembers(): { person: string; name: string }[] {
  const client = useContext(QueryClientContext)
  const all = client?.getQueryData<Workspace[]>(['workspaces']) ?? []
  return all.flatMap(w => w.members.filter(m => !m.name_hidden).map(m => ({ person: m.person, name: m.name })))
}
/**
 * How a person id reads in the plain view. Names are self-chosen, so the name never stands alone: it comes WITH the short
 * id, and only for an EXACT full id of a known member (never a prefix). An unknown id, a cut id or a name two different
 * people share shows the short id plus why there is no name. Null when the value is not a person id.
 */
export function personLabel(value: string | null, members: { person: string; name: string }[]): { text: string; full: string | null } | null {
  if (!value || !PERSON.test(value)) return null
  const hit = members.find(x => x.person === value)
  if (!hit) return { text: `${value.endsWith('…') || value.endsWith('...') ? value : shortId(value)} (unknown person)`, full: null }
  const sharing = new Set(members.filter(x => x.name === hit.name).map(x => x.person)).size
  if (sharing > 1) return { text: `${shortId(value)} (name shared by ${sharing} members)`, full: value }
  return { text: `${hit.name} (${shortId(value)})`, full: value }
}
export function SignContent({ content }: { content: Content }) {
  const members = useKnownMembers()
  const shown = (v: string | null) => v === null ? null : personLabel(v, members)?.text ?? v
  const named = (content.fields_changed ?? []).flatMap(f => [f.before, f.after]).filter((v): v is string => personLabel(v, members)?.full != null)
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
      {[...new Set(named)].map(id => <p key={id}><Raw>{personLabel(id, members)!.text}</Raw> = <Raw>{id}</Raw></p>)}
    </details>}
  </div>
}
