import { QueryClientContext } from '@tanstack/react-query'
import { ShieldAlert } from 'lucide-react'
import { useContext, useState, type ReactNode } from 'react'
import type { Workspace } from '@/api/types'
import type { SignContent as Content } from '@/api/signing'
import { GATE_LABEL } from '@/api/settings'
import { Pictures } from './Pictures'
import { Inline, Prose, Raw } from './visible'
import { alsoSigned, type AlsoSigned, type QuickTyped } from './alsoSigned'
import { SignedTextView } from './SignedTextView'

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

/**
 * The fields a compact answer review needs: the ticket (an existing one), the question, the chosen option. Null when any
 * is missing or the host sent a summary: the caller then shows the full review. Never a compact view that hides content.
 */
export function compactAnswer(content: Content | null | undefined) {
  if (!content || content.summary || !content.ticket || content.ticket.new === true) return null
  const choice = content.fields_changed?.find(f => f.field === 'your choice')
  const question = content.sections?.find(s => s.id === 'question')
  if (!choice || choice.after == null || !question) return null
  return { ticket: content.ticket, choice, question, notes: content.sections?.filter(s => s.id === 'answer') ?? [] }
}

/** A deny-list, never an allow-list: in a gate review every section except core's own is ticket text (the ticket's section keys, `findings`, an add-on's) and sits in the agent frame. */
const CORE_SECTIONS = ['comment'] // sections core writes itself (the signer's own comment): every OTHER section of a gate review is agent text
/** orch's own words for the known section ids (renaming is presentation); an unknown id shows core's title. */
const PLAIN_TITLES: Record<string, string> = { context: 'What it is about', requirements: 'What will be built', out_of_scope: 'Out of scope', plan: 'Steps' }
const SHOW_RAW_KEY = 'orch.sign.showRaw'
let showRawNow = false
try { showRawNow = sessionStorage.getItem(SHOW_RAW_KEY) === '1' } catch { /* no storage: the default */ }

/**
 * Requirements and plan, easier to read and still exactly what is signed: one labelled frame "Written by the agent" holds
 * every agent-written section, each line restyled by its leading marker only (SignedTextView); "Show exactly as written"
 * swaps in core's own text per section. The frame label, section titles and "What happens when you sign" are orch's words.
 */
function GateText({ sections, acceptance, tasks, approving, others }: { sections: NonNullable<Content['sections']>; acceptance?: ReactNode; tasks?: ReactNode; approving: boolean; others?: ReactNode }) {
  const [raw, setRaw] = useState(showRawNow)
  const toggle = (v: boolean) => { showRawNow = v; setRaw(v); try { sessionStorage.setItem(SHOW_RAW_KEY, v ? '1' : '0') } catch { /* no storage */ } }
  return <div className="space-y-3">
    <section role="group" aria-label="Written by the agent" data-testid="agent-frame" className="space-y-4 rounded-md border border-border-strong bg-surface p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-[12px] font-medium text-text-muted">Written by the agent: {approving ? 'this is what you approve' : 'this is what your decision covers'}</p>
        <label className="flex cursor-pointer items-center gap-1.5 text-[12px] text-text-muted"><input type="checkbox" checked={raw} onChange={e => toggle(e.target.checked)} />Show exactly as written</label>
      </div>
      {sections.map(s => <section key={s.id} data-section={s.id}>
        <h3 className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-text-muted">{PLAIN_TITLES[s.id] ?? <Inline>{s.title}</Inline>}</h3>
        {raw ? <Prose className="rounded-md bg-surface-2 p-2 font-mono text-[12px] leading-relaxed text-text">{s.text}</Prose> : <SignedTextView text={s.text} />}
      </section>)}
      {acceptance}
      {tasks}
    </section>
    {others}
    {approving && <section aria-label="What happens when you sign" className="space-y-1 text-[12px] text-text-muted">
      <h3 className="font-medium text-text">What happens when you sign</h3>
      <ul className="list-disc space-y-0.5 pl-5">
        <li>Your signature is tied to exactly what is shown in this review.</li>
        <li>If the agent changes it later, this approval no longer counts and you are asked again.</li>
        <li>Only you can sign with your key. The agent cannot sign for you.</li>
      </ul>
    </section>}
  </div>
}

/** `technical`: more facts for the ONE "Technical details" expander of the review (the panel's own: action, fingerprint, device). */
/** A row of the quick-task review: a label and the text signed for it (one line stays a line, several keep their breaks). */
function SignedRow({ label, text, members }: { label: string; text: string; /** With members: the text is a person id. */ members?: Member[] }) {
  if (members) return <div className="grid min-w-0 grid-cols-1 gap-x-4 gap-y-0.5 sm:grid-cols-[8rem_minmax(0,1fr)]"><dt className="text-text-muted"><Inline>{label}</Inline></dt><dd className="min-w-0 text-text"><Shown plain value={text} members={members} /></dd></div>
  return <div className="grid min-w-0 grid-cols-1 gap-x-4 gap-y-0.5 sm:grid-cols-[8rem_minmax(0,1fr)]"><dt className="text-text-muted"><Inline>{label}</Inline></dt><dd className="min-w-0 text-text">{text.includes('\n') ? <Prose className="font-sans leading-relaxed text-text">{text}</Prose> : <Inline>{text}</Inline>}</dd></div>
}

/**
 * The quick task: what was typed once, the sentence that more is approved, and the derived rows folded under a count.
 * Only used when the whole review is exactly the derived one (alsoSigned); otherwise the caller shows the full review.
 */
function QuickSigned({ typed, also, members, notes = [] }: { typed: QuickTyped; also: AlsoSigned; members: Member[]; /** The host's own sentences: kept, but inside the fold. */ notes?: string[] }) {
  const owner = also.rows.find(r => r.person)
  const type = also.rows.find(r => r.label === 'Type')?.text
  const checks = typed.checks
  const details = typed.details.trim()
  return <>
    <blockquote data-testid="sign-typed" aria-label="What you typed" className="space-y-1.5 rounded-md border-l-2 border-brand bg-surface-2 px-3 py-2 text-text">
      <p className="font-medium"><Inline>{typed.title}</Inline></p>
      {details && details !== typed.title && <Prose className="font-sans leading-relaxed text-text">{details}</Prose>}
      {checks.length > 0 && <ul className="space-y-0.5 text-text-muted">{checks.map((c, i) => <li key={i}>Check: <Inline>{c.text}</Inline>{c.cmd ? <> (checked by <Inline>{c.cmd}</Inline>)</> : null}</li>)}</ul>}
    </blockquote>
    <ul className="space-y-1 text-text">
      <li data-testid="sign-also-sentence">Counts as approved: what to do and how to check it.</li>
      <li>{type ?? 'Task'}{owner ? <>, owner <Shown plain value={owner.text} members={members} /></> : null}.</li>
    </ul>
    <div data-testid="sign-also" className="space-y-2">
      <dl className="mt-2 space-y-2">{also.rows.map((r, i) => <SignedRow key={i} label={r.label} text={r.text} members={r.person ? members : undefined} />)}</dl>
      {notes.map((n, i) => <p key={i} className="mt-2 text-text-muted"><Prose inline>{n}</Prose></p>)}
    </div>
  </>
}

export function SignContent({ content, ws, technical, compact = false, quick, workspaceLine }: { content: Content; ws?: string; technical?: ReactNode; /** The workspace line, kept one click away inside this review's own fold (hub join) instead of a second fold of its own. */ workspaceLine?: ReactNode; compact?: boolean; /** A quick task: the typed values, for the folded "Also signed" view. */ quick?: QuickTyped }) {
  const members = useWorkspaceMembers(ws)
  // Every signed person id appears in full somewhere in the view: the plain view shows it short, this list in full.
  const ids = [...new Set((content.fields_changed ?? []).flatMap(f => [f.before, f.after]).filter((v): v is string => personLabel(v, members)?.full != null))]
  const cancels = cancellationRows(content.cancels)
  const summary = content.summary
  // A request on an EXISTING ticket keeps its key (the host's word) and the end of the signed id in the main view; a creation
  // (it signs the key and the title) has them under Technical details only.
  const also = quick && !content.summary ? alsoSigned(content, quick) : null
  // core sets fields_title 'Applies under' only on a gate review (signcontent.py _gate): the structural mark, not the headline wording.
  const gate = content.fields_title === 'Applies under' && !!content.sections?.length
  const gateSections = content.sections?.filter(s => !CORE_SECTIONS.includes(s.id)) ?? []
  const otherSections = content.sections?.filter(s => CORE_SECTIONS.includes(s.id)).map(s => <section key={s.id}><h3 className="mb-2 text-[13px] font-medium text-text"><Inline>{s.title}</Inline></h3><Prose className="font-sans leading-relaxed text-text">{s.text}</Prose></section>)
  const acceptanceEl = !!content.acceptance?.length && <section><h3 className="mb-2 text-[13px] font-medium text-text">{gate ? 'How we know it is done' : 'Acceptance criteria'}</h3><ul className="space-y-2 leading-relaxed">{content.acceptance.map(a => <li key={a.id}><span aria-label={a.checked ? 'Checked' : 'Unchecked'}>{a.checked ? '☑' : '☐'}</span> <Inline>{a.text}</Inline> <span className="text-[12px] text-text-muted [&_bdi]:text-text-muted"><Raw>{a.id}</Raw></span>{a.manual && <p className="text-[12px] text-text-muted"><Inline>{a.manual}</Inline></p>}</li>)}</ul></section>
  const tasksEl = !!content.tasks?.length && <section><h3 className="mb-2 text-[13px] font-medium text-text">{gate ? 'Steps' : 'Plan tasks'}</h3><ol className="list-decimal space-y-3 pl-5 leading-relaxed">{content.tasks.map(t => <li key={t.n} value={t.n}><Inline>{t.title}</Inline> <span className="text-[12px] text-text-muted [&_bdi]:text-text-muted"><Raw>{t.id}</Raw></span>{t.verify?.map((v,i) => <p key={i} className="mt-1 font-mono text-[12px] leading-relaxed text-text-muted [&_bdi]:text-text-muted">Verify: <Value>{v}</Value></p>)}</li>)}</ol></section>
  const fields = !also && !!content.fields_changed?.length && <section><h3 className="mb-2 text-[13px] font-medium text-text"><Inline>{content.fields_title ?? (content.fields_changed.every(f => f.before == null) ? 'Signed values' : 'Changed fields')}</Inline></h3><dl className="space-y-3">{content.fields_changed.map(f => <div key={f.field} className="grid min-w-0 grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-[7rem_minmax(0,1fr)]"><dt className="text-text-muted"><Inline>{`${cap(f.field)}:`}</Inline></dt> <dd className="min-w-0 text-text [&_bdi]:font-sans [&_bdi]:text-[13px]">{f.before == null ? null : <><Shown plain value={f.before} members={members} />{f.before_note && <span className="text-text-muted"> (<Raw>{f.before_note}</Raw>)</span>} → </>}<Shown plain value={f.field === 'type' && f.after ? (TYPE_WORDS[f.after] ?? f.after) : f.after} members={members} empty="(not set)" /></dd></div>)}</dl></section>
  const rest = <>
    {content.fields_inline && fields}

    {gate ? <GateText sections={gateSections} approving={/^Approve /.test(content.headline ?? '')} acceptance={acceptanceEl} tasks={tasksEl} others={otherSections} /> : null}
    {!gate && !also && content.sections?.map(s => <section key={s.id} aria-label={s.title}><h3 className="mb-2 text-[13px] font-medium text-text"><Inline>{s.title}</Inline></h3><Prose className="font-sans leading-relaxed text-text">{s.text}</Prose></section>)}
    <Pictures images={content.images} />
    {!gate && !also && acceptanceEl}
    {!gate && !also && tasksEl}

    {cancels.length > 0 && <section aria-label="Signing cancels">
      <h3 className="text-[13px] font-medium">Signing cancels:</h3>
      <ul>{cancels.map((c, i) => <li key={i}>
        {GATE_LABEL[c.gate as keyof typeof GATE_LABEL]} approval ({c.people.map((p, j) => <span key={j}>{j > 0 ? ', ' : ''}<Inline>{personLabel(p, members)?.name ?? 'Unknown person'}</Inline></span>)}){!c.complete && ' · not yet complete'}
      </li>)}</ul>
      <p className="text-[12px] text-text-muted">(per host, not signed)</p>
    </section>}
    <div className="space-y-2 border-t border-border pt-4 empty:hidden">{[...(also ? [] : content.notes ?? []).filter(n => n !== summary?.risk), ...(Array.isArray(content.note) ? content.note : content.note ? [content.note] : [])].map((note, i) => <p key={i} className="text-[13px] leading-relaxed text-text-muted"><Prose inline>{note}</Prose></p>)}</div>
    {(content.ticket || ids.length > 0 || technical || content.fields_changed?.length || summary) && <details className="space-y-3 text-[12px] text-text-muted">
      <summary className="cursor-pointer">{workspaceLine && !summary ? 'Details' : 'Technical details'}</summary>
      {!summary && workspaceLine}
      {also && quick && <QuickSigned typed={quick} also={also} members={members} notes={content.notes ?? []} />}
      {content.ticket && <div>
        {<p><Raw>{content.ticket.key}</Raw> <span className="text-text-muted">(key per host)</span> · signed id …<Raw>{content.ticket.uid_tail}</Raw></p>}
        {content.ticket.title && <Value>{content.ticket.title}</Value>}
        {content.ticket.key_note && <p><Value>{content.ticket.key_note}</Value></p>}
      </div>}
      {!content.fields_inline && fields}

      {ids.map(id => <p key={id}><Person parts={personLabel(id, members)!} /> = <Raw>{id}</Raw></p>)}
      {technical}
    </details>}
  </>
  const parts = compact ? compactAnswer(content) : null
  const choice = parts?.choice.after
  const facts = summary?.lines ?? []
  return <div data-testid={parts ? 'sign-compact' : undefined} className="min-w-0 space-y-3 font-sans text-[13px] leading-relaxed text-text [overflow-wrap:anywhere]">
    {choice && <p data-testid="sign-choice">Your answer will be <Inline>{choice}</Inline>.</p>}
    {facts.length > 0 && <section data-testid="sign-summary"><ul className="space-y-1.5">{facts.map((line, i) => <li key={i}><Inline>{line}</Inline></li>)}</ul></section>}
    {summary && content.ticket && <p data-testid="sign-ticket"><Raw>{content.ticket.key}</Raw> · signed id …<Raw>{content.ticket.uid_tail}</Raw></p>}
    {summary?.risk && <p role="note" data-testid="sign-risk" className="flex items-start gap-2 rounded-md border border-warning/50 border-l-[3px] border-l-risk-bar bg-warning-soft px-3 py-2 font-medium text-text"><ShieldAlert className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden /><span><Inline>{summary.risk}</Inline></span></p>}
    {!summary ? <div data-testid="sign-exact" className="min-w-0 space-y-5">{rest}</div> : <details data-testid="sign-exact" className="space-y-3 rounded-md border border-border p-3">
      <summary className="cursor-pointer text-[12px] font-medium text-text-muted">What you sign</summary>
      <div className="mt-2 min-w-0 space-y-5">
        {workspaceLine}
        {rest}
      </div>
    </details>}
  </div>
}
