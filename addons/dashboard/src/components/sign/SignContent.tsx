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
/** A person id as the host writes it: the full `p_<hex>`, or one cut by an older host (`p_181d953f…`). */
const PERSON = /^p_[0-9a-f]+(…|\.\.\.)?$/
/** `p_` plus 12 hex characters: long enough to tell people apart, never the whole id. */
const shortId = (id: string) => (id.length > 14 ? `${id.slice(0, 14)}…` : id)
const CONTROLS = /[\u200B-\u200F\u202A-\u202E\u2066-\u2069]/g
/** A name as it is shown: zero-width and bidi control characters removed, so it cannot reorder the text around it. */
export const cleanName = (name: string) => name.replace(CONTROLS, '')
/** The one form a name is judged and shown in: controls removed, NFKC, whitespace collapsed. */
const canonName = (name: string) => cleanName(name).normalize('NFKC').trim().replace(/\s+/g, ' ')
/**
 * DESIGN RULE: the 12-hex id is the identity; a name is only a hint, and shown only when it is unambiguous.
 * "Unambiguous" is judged on a skeleton (UTS #39 subset): NFKC, casefold, Cyrillic/Greek look-alikes mapped to Latin,
 * combining marks dropped. Two members whose skeletons are equal share a name, however they spell it.
 */
const LOOKALIKES: Record<string, string> = Object.fromEntries(Object.entries({
  // lowercase look-alikes (mapped on the original-case string, BEFORE any casefolding: Greek capital Ν is N, small ν is v)
  a: 'аα', b: 'вβ', c: 'сϲ', d: 'ԁ', e: 'еε', h: 'нһ', i: 'іιlӏ', j: 'јϳ', k: 'кκ', m: 'мμ', n: 'η', o: 'оο', p: 'рρ',
  q: 'ԛ', r: 'г', s: 'ѕ', t: 'тτ', u: 'υ', v: 'νѵ', w: 'ԝω', x: 'хχ', y: 'уүγ', z: 'ζ',
  // capitals
  A: 'АΑ', B: 'ВΒ', C: 'С', E: 'ЕΕ', H: 'НΗ', I: 'ІΙ', J: 'Ј', K: 'КΚ', M: 'МΜ', N: 'Ν', O: 'ОΟ', P: 'РΡ', S: 'Ѕ', T: 'ТΤ', X: 'ХΧ', Y: 'ΥҮ', Z: 'Ζ',
}).flatMap(([latin, many]) => [...many].map(c => [c, latin])))
const nameKey = (name: string) => [...canonName(name)].map(c => LOOKALIKES[c] ?? c).join('').toLowerCase().toUpperCase().toLowerCase().normalize('NFD').replace(/\p{M}/gu, '')
/**
 * An ALLOWLIST, not a blocklist: a name is shown only when, after NFKC, it is letters and marks (plus space . ' -) of at
 * most 40 characters, in ONE script among those that have look-alike letters (no Latin + Cyrillic/Greek mixing), with no
 * enclosing mark and at most 2 combining marks in a row. Brackets of any width, digits, symbols, emoji and the ellipsis
 * can therefore never appear in a name.
 */
const NAME_OK = /^[\p{L}\p{M}][\p{L}\p{M} .'\-]{0,39}$/u
const LOOKALIKE_SCRIPTS = [/\p{Script=Latin}/u, /\p{Script=Cyrillic}/u, /\p{Script=Greek}/u, /\p{Script=Armenian}/u, /\p{Script=Cherokee}/u, /\p{Script=Coptic}/u]
export function showableName(name: string): string | null {
  const n = canonName(name)
  if (/\p{Default_Ignorable_Code_Point}/u.test(n) || !NAME_OK.test(n) || /\.{2,}/.test(n) || /\p{Me}/u.test(n) || /\p{M}{3,}/u.test(n.normalize('NFD'))) return null // NFKC turns an ellipsis into "...": no run of dots either
  // Covered letters (Latin, or a mapped look-alike) mixed with letters the table does not cover: not judgeable, not shown.
  const letters = [...n].filter(c => /\p{L}/u.test(c))
  const covered = (c: string) => /\p{Script=Latin}/u.test(c) || c in LOOKALIKES
  if (letters.some(covered) && !letters.every(covered)) return null
  return LOOKALIKE_SCRIPTS.filter(re => re.test(n)).length > 1 ? null : n
}

export interface Member { person: string; name: string; hidden?: boolean }
/** What the plain view shows for a person id. `full` is the id in full when the value is not cut, so it is always inspectable. */
export interface PersonParts { id: string; short: string; name: string | null; note: string | null; full: string | null }

/**
 * THE only place a person id becomes display parts. `members` are the members of THE request's workspace only.
 * A name appears only for an EXACT full id of a known member, only when it is unique after normalisation, visible and
 * cannot fake an id; it always comes with the short id. Everything else shows the short id and says why there is no name.
 * Null when the value is not a person id.
 */
export function personLabel(value: string | null, members: Member[]): PersonParts | null {
  if (!value || !PERSON.test(value)) return null
  const cut = value.endsWith('…') || value.endsWith('...')
  const base = { id: value, short: cut ? value : shortId(value), name: null, full: cut ? null : value }
  if (cut) return { ...base, note: 'unknown person' }
  const hit = members.find(x => x.person === value)
  if (!hit) return { ...base, note: 'unknown person' }
  if (hit.hidden) return { ...base, note: 'name not shown' }
  const key = nameKey(hit.name)
  const sharing = new Set(members.filter(x => !x.hidden && nameKey(x.name) === key).map(x => x.person)).size
  if (sharing > 1) return { ...base, note: `name shared by ${sharing} members` }
  const name = showableName(hit.name)
  if (name === null) return { ...base, note: 'name not shown' }
  return { ...base, name, note: null }
}

/** The members of the request's workspace from what the dashboard already loaded (no fetch, no provider needed in tests). */
export function useWorkspaceMembers(ws: string | undefined): Member[] {
  const client = useContext(QueryClientContext)
  const all = client?.getQueryData<Workspace[]>(['workspaces']) ?? []
  return (all.find(w => w.id === ws)?.members ?? []).map(m => ({ person: m.person, name: m.name, hidden: !!m.name_hidden }))
}

/** Name and id as separate isolates, so neither can reorder or hide the other. */
function Person({ parts }: { parts: PersonParts }) {
  const id = <bdi dir="ltr" className="whitespace-pre-wrap break-all font-mono text-[12px] text-text [unicode-bidi:isolate]">{parts.short}</bdi>
  if (parts.name !== null) return <><Raw>{parts.name}</Raw> ({id})</>
  return <>{id} ({parts.note})</>
}
function Shown({ value, members }: { value: string | null; members: Member[] }) {
  const parts = personLabel(value, members)
  if (parts) return <Person parts={parts} />
  return <Value>{value ?? '—'}</Value>
}
export function SignContent({ content, ws }: { content: Content; ws?: string }) {
  const members = useWorkspaceMembers(ws)
  // Every signed person id appears in full somewhere in the view: the plain view shows it short, this list in full.
  const ids = [...new Set((content.fields_changed ?? []).flatMap(f => [f.before, f.after]).filter((v): v is string => personLabel(v, members)?.full != null))]
  return <div className="max-h-[38vh] space-y-4 overflow-auto text-[13px] [overflow-wrap:anywhere]">
    {content.ticket && <div>
      <p><Raw>{content.ticket.key}</Raw> <span className="text-text-muted">(key per host)</span> · signed id …<Raw>{content.ticket.uid_tail}</Raw></p>
      {content.ticket.title && <Value>{content.ticket.title}</Value>}
    </div>}
    {content.sections?.map(s => <section key={s.id}><h3 className="mb-1 text-[12px] font-medium text-text-muted"><Raw>{s.title}</Raw></h3><Value block>{s.text}</Value></section>)}
    {!!content.acceptance?.length && <section><h3 className="text-text-muted">Acceptance criteria</h3><ul className="space-y-1">{content.acceptance.map(a => <li key={a.id}><span aria-label={a.checked ? 'Checked' : 'Unchecked'}>{a.checked ? '☑' : '☐'}</span> <Raw>{a.id}</Raw> · <Value>{a.text}</Value></li>)}</ul></section>}
    {!!content.tasks?.length && <section><h3 className="text-text-muted">Plan tasks</h3><ol className="list-decimal space-y-2 pl-5">{content.tasks.map(t => <li key={t.n} value={t.n}><Raw>{t.id}</Raw> · <Value>{t.title}</Value>{t.verify?.map((v,i) => <p key={i} className="font-mono text-[12px]">Verify: <Value>{v}</Value></p>)}</li>)}</ol></section>}
    {!!content.fields_changed?.length && <section><h3 className="text-text-muted"><Raw>{content.fields_title ?? 'Changed fields'}</Raw></h3>{content.fields_changed.map(f => <p key={f.field}><Raw>{f.field}</Raw>: {content.fields_title && f.before == null ? null : <><Shown value={f.before} members={members} />{f.before_note && <span className="text-text-muted"> (<Raw>{f.before_note}</Raw>)</span>} → </>}<Shown value={f.after} members={members} /></p>)}</section>}
    {[...(content.notes ?? []), ...(Array.isArray(content.note) ? content.note : content.note ? [content.note] : [])].map((note, i) => <p key={i}><Value>{note}</Value></p>)}
    {(content.ticket?.key_note || ids.length > 0) && <details className="text-[12px] text-text-muted">
      <summary className="cursor-pointer">Technical details: ids and keys</summary>
      {content.ticket?.key_note && <p><Value>{content.ticket.key_note}</Value></p>}
      {ids.map(id => <p key={id}><Person parts={personLabel(id, members)!} /> = <Raw>{id}</Raw></p>)}
    </details>}
  </div>
}
