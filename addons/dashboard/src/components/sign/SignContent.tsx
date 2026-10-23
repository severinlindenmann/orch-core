import { QueryClientContext } from '@tanstack/react-query'
import { useContext, type ReactNode } from 'react'
import type { Workspace } from '@/api/types'
import type { SignContent as Content } from '@/api/signing'
import { GATE_LABEL } from '@/api/settings'
import { Pictures } from './Pictures'
import { Inline, Prose, Raw } from './visible'

export function SignDetails({ children }: { children: ReactNode }) {
  return <details className="space-y-3 text-[12px] text-text-muted"><summary className="cursor-pointer">Technical details</summary>{children}</details>
}
/** The first letter as a capital: a field name as a label ("type" -> "Type"). Display only; the signed text is untouched. */
/** The ticket types as words; any other value is shown exactly as signed. */
const TYPE_WORDS: Record<string, string> = { feature: 'Feature', bug: 'Bug', chore: 'Chore', spike: 'Spike', epic: 'Epic' }
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)
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
const mapLookalikes = (text: string) => [...text].map(c => LOOKALIKES[c] ?? c).join('')
// Order matters (a mark whose case mapping is a letter, U+0345 -> ι, is a letter only after folding): map look-alikes on the
// original case, fold case, NFD and drop marks, map again. host/personlabel.py `_key` does the same steps.
const nameKey = (name: string) => mapLookalikes(mapLookalikes(canonName(name)).toLowerCase().toUpperCase().toLowerCase().normalize('NFD').replace(/\p{M}/gu, '')).toLowerCase()
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

/**
 * Name and id as separate isolates, so neither can reorder or hide the other. `plain` (the main view): a name that passed
 * every rule above is shown alone; the id it stands for is one line away in the Technical details (`name (id) = full id`).
 * A person without a showable name always keeps the id and the reason. Next to a name the main view always keeps the same
 * 12-hex short id the details use, muted ("Severin · p_181d953f12ab…"): names can look alike, so who is meant stays
 * visible without opening the details.
 */
function Person({ parts, plain = false }: { parts: PersonParts; plain?: boolean }) {
  const id = <bdi dir="ltr" className="whitespace-pre-wrap break-all font-mono text-[12px] text-text [unicode-bidi:isolate]">{parts.short}</bdi>
  if (parts.name !== null) return plain
    ? <><Inline>{parts.name}</Inline> <bdi dir="ltr" data-testid="person-tail" title="This person's id (the same short id the Technical details use)" className="font-mono text-[11px] text-text-muted [unicode-bidi:isolate]">· {parts.short}</bdi></>
    : <><Raw>{parts.name}</Raw> ({id})</>
  return <>{id} ({parts.note})</>
}
function Shown({ value, members, empty = '—', plain = false }: { value: string | null; members: Member[]; empty?: string; plain?: boolean }) {
  const parts = personLabel(value, members)
  if (parts) return <Person parts={parts} plain={plain} />
  if (plain && value !== null && !value.includes('\n')) return <Inline>{value}</Inline>
  return <Value>{value ?? empty}</Value>
}
/** Host context only. Malformed or unknown entries must never break the signing surface. */
function cancellationRows(value: unknown): NonNullable<Content['cancels']> {
  if (!Array.isArray(value) || !value.every(row => row && typeof row === 'object'
    && Object.keys(row).length === 3 && typeof row.gate === 'string' && Object.hasOwn(GATE_LABEL, row.gate)
    && typeof row.complete === 'boolean' && Array.isArray(row.people) && row.people.length > 0
    && row.people.every((p: unknown) => typeof p === 'string' && /^p_[0-9a-f]{32}$/.test(p)))) return []
  return value
}

/** `technical`: more facts for the ONE "Technical details" expander of the review (the panel's own: action, fingerprint, device). */
export function SignContent({ content, ws, technical, compact = false }: { content: Content; ws?: string; technical?: ReactNode; compact?: boolean }) {
  const members = useWorkspaceMembers(ws)
  // Every signed person id appears in full somewhere in the view: the plain view shows it short, this list in full.
  const ids = [...new Set((content.fields_changed ?? []).flatMap(f => [f.before, f.after]).filter((v): v is string => personLabel(v, members)?.full != null))]
  const cancels = cancellationRows(content.cancels)
  const summary = content.summary
  // A request on an EXISTING ticket keeps its key (the host's word) and the end of the signed id in the main view; a creation
  // (it signs the key and the title) has them under Technical details only.
  const ticketLine = content.ticket && content.ticket.new !== true
  const rest = <>
    {ticketLine && content.ticket && <p data-testid="sign-ticket" className="text-[12px] text-text-muted"><Raw>{content.ticket.key}</Raw> (key per host) · signed id …<Raw>{content.ticket.uid_tail}</Raw></p>}
    {content.sections?.map(s => <section key={s.id}><h3 className="mb-2 text-[13px] font-medium text-text"><Inline>{s.title}</Inline></h3><Prose className="font-sans leading-relaxed text-text">{s.text}</Prose></section>)}
    <Pictures images={content.images} />
    {!!content.acceptance?.length && <section><h3 className="mb-2 text-[13px] font-medium text-text">Acceptance criteria</h3><ul className="space-y-2 leading-relaxed">{content.acceptance.map(a => <li key={a.id}><span aria-label={a.checked ? 'Checked' : 'Unchecked'}>{a.checked ? '☑' : '☐'}</span> <Inline>{a.text}</Inline> <span className="text-[12px] text-text-muted [&_bdi]:text-text-muted"><Raw>{a.id}</Raw></span>{a.manual && <p className="text-[12px] text-text-muted"><Inline>{a.manual}</Inline></p>}</li>)}</ul></section>}
    {!!content.tasks?.length && <section><h3 className="mb-2 text-[13px] font-medium text-text">Plan tasks</h3><ol className="list-decimal space-y-3 pl-5 leading-relaxed">{content.tasks.map(t => <li key={t.n} value={t.n}><Inline>{t.title}</Inline> <span className="text-[12px] text-text-muted [&_bdi]:text-text-muted"><Raw>{t.id}</Raw></span>{t.verify?.map((v,i) => <p key={i} className="mt-1 font-mono text-[12px] leading-relaxed text-text-muted [&_bdi]:text-text-muted">Verify: <Value>{v}</Value></p>)}</li>)}</ol></section>}
    {!!content.fields_changed?.length && <section><h3 className="mb-2 text-[13px] font-medium text-text"><Inline>{content.fields_title ?? 'Changed fields'}</Inline></h3><dl className="space-y-3">{content.fields_changed.map(f => <div key={f.field} className="grid min-w-0 grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-[7rem_minmax(0,1fr)]"><dt className="text-text-muted"><Inline>{`${cap(f.field)}:`}</Inline></dt> <dd className="min-w-0 text-text [&_bdi]:font-sans [&_bdi]:text-[13px]">{(content.fields_title || /: (add|remove)$/.test(f.field) || / \(unchanged, per host\)$/.test(f.field)) && f.before == null ? null : <><Shown plain value={f.before} members={members} />{f.before_note && <span className="text-text-muted"> (<Raw>{f.before_note}</Raw>)</span>} → </>}<Shown plain value={f.field === 'type' && f.after ? (TYPE_WORDS[f.after] ?? f.after) : f.after} members={members} empty="(not set)" /></dd></div>)}</dl></section>}
    {cancels.length > 0 && <section aria-label="Signing cancels">
      <h3 className="text-[13px] font-medium">Signing cancels:</h3>
      <ul>{cancels.map((c, i) => <li key={i}>
        {GATE_LABEL[c.gate as keyof typeof GATE_LABEL]} approval ({c.people.map((p, j) => <span key={j}>{j > 0 ? ', ' : ''}<Inline>{personLabel(p, members)?.name ?? 'Unknown person'}</Inline></span>)}){!c.complete && ' · not yet complete'}
      </li>)}</ul>
      <p className="text-[12px] text-text-muted">(per host, not signed)</p>
    </section>}
    <div className="space-y-2 border-t border-border pt-4 empty:hidden">{[...(content.notes ?? []), ...(Array.isArray(content.note) ? content.note : content.note ? [content.note] : [])].map((note, i) => <p key={i} className="text-[13px] leading-relaxed text-text-muted"><Prose inline>{note}</Prose></p>)}</div>
    {(content.ticket || ids.length > 0 || technical) && <details className="space-y-3 text-[12px] text-text-muted">
      <summary className="cursor-pointer">Technical details</summary>
      {content.ticket && <div>
        {!ticketLine && <p><Raw>{content.ticket.key}</Raw> <span className="text-text-muted">(key per host)</span> · signed id …<Raw>{content.ticket.uid_tail}</Raw></p>}
        {content.ticket.title && <Value>{content.ticket.title}</Value>}
        {content.ticket.key_note && <p><Value>{content.ticket.key_note}</Value></p>}
      </div>}
      {ids.map(id => <p key={id}><Person parts={personLabel(id, members)!} /> = <Raw>{id}</Raw></p>)}
      {technical}
    </details>}
  </>
  // Compact (an answer): what is answered stays in view (the choice row and the typed note); the question, the other
  // options and the host's facts are all still there, in full, under Details. Nothing is left out of the signed values.
  if (compact && !summary) {
    const mine = content.fields_changed?.filter(f => f.field === 'your choice') ?? []
    const note = content.sections?.filter(s => s.id === 'answer') ?? []
    return <div className="min-w-0 space-y-3 font-sans text-[13px] leading-relaxed text-text [overflow-wrap:anywhere]">
      {mine.map(f => <p key={f.field} data-testid="sign-choice" className="text-text"><span className="text-text-muted">Your answer: </span><Shown plain value={f.after} members={members} /></p>)}
      {note.map(s => <section key={s.id}><h3 className="mb-1 text-[12px] font-medium text-text-muted"><Inline>{s.title}</Inline></h3><Prose className="font-sans leading-relaxed text-text">{s.text}</Prose></section>)}
      <details data-testid="sign-exact" className="space-y-3 rounded-md border border-border p-3">
        <summary className="cursor-pointer text-[12px] font-medium text-text-muted">Details: everything you sign</summary>
        <div className="mt-2 max-h-[38vh] min-w-0 space-y-5 overflow-auto pr-3">{rest}</div>
      </details>
    </div>
  }
  return <div className="min-w-0 max-h-[38vh] space-y-5 overflow-auto pr-3 font-sans text-[13px] leading-relaxed text-text [overflow-wrap:anywhere]">
    {summary ? <>
      <section data-testid="sign-summary" aria-label={summary.title} className="space-y-2">
        <h3 className="text-[13px] font-medium text-text"><Inline>{summary.title}</Inline></h3>
        <ul className="list-disc space-y-2 pl-5 text-text">{summary.lines.map((line, i) => <li key={i}><Inline>{line}</Inline></li>)}</ul>
        <p className="text-[12px] text-text-muted">This summary is worked out from the signed values below. Those exact values are what you sign, and they are shown in full.</p>
      </section>
      <details open data-testid="sign-exact" className="space-y-3 rounded-md border border-border p-3 sm:p-4">
        <summary className="cursor-pointer text-[12px] font-medium text-text-muted">Exact signed details</summary>
        <div className="mt-2 min-w-0 space-y-5">{rest}</div>
      </details>
    </> : rest}
  </div>
}
