import { QueryClientContext } from '@tanstack/react-query'
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
/** Rows whose values are shown exactly as signed, never looked up as a member (an option title can equal a person id). */
const verbatimRow = (field: string) => field === 'recommended' || field === 'question hash' || field.startsWith('option ')
function Shown({ value, members, empty = '—', plain = false, verbatim = false }: { value: string | null; members: Member[]; empty?: string; plain?: boolean; verbatim?: boolean }) {
  const parts = verbatim ? null : personLabel(value, members)
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

/** Someone else's long text, shortened on screen: runs of blank lines collapsed, at most ~6 lines until "Show all". Display only. */
function ClampedProse({ children }: { children: string }) {
  const [all, setAll] = useState(false)
  const flat = children.replace(/\n[ \t]*(\n[ \t]*)+/g, '\n')
  const long = flat.length > 280 || flat.split('\n').length > 6
  return <>
    <div className={!all && long ? 'max-h-32 overflow-hidden' : undefined}><Prose className="font-sans leading-relaxed text-text">{flat}</Prose></div>
    {long && <button type="button" onClick={() => setAll(a => !a)} className="mt-1 text-[12px] text-text-muted underline underline-offset-4">{all ? 'Show less' : 'Show all'}</button>}
  </>
}

/** The sections the compact answer review draws itself (the full list below it leaves them out). */
const COMPACT_IDS = ['selected', 'answer', 'note', 'question', 'details']

/** The signer View's `question hash`: the ONE contract row. Null when there are none or more than one (never a guess). */
export function viewQuestionHash(content: Content | null | undefined): string | null {
  const rows = (content?.fields_changed ?? []).filter(f => f.field === 'question hash')
  return rows.length === 1 && typeof rows[0].after === 'string' && rows[0].after ? rows[0].after : null
}

const OPTION_KEY = '[a-z][a-z0-9_]*'
const OPTION_HEAD = new RegExp(`^([A-F]) · key (${OPTION_KEY})$`)

/** Whether the View's rows are exactly the frozen set, in order: `recommended`, the allow-other row, one `option <key>` row per
 * option (letters A, B, C by stored order) each followed only by its own `title` (always), `text`, `fact N`, `cost` rows in that
 * order, the two routing rows, and `question hash` last. Returns the option keys, or null for any unknown/missing/extra row. */
function frozenRows(rows: NonNullable<Content['fields_changed']>): { keys: string[]; recommended: string | null } | null {
  let i = 0
  const keys: string[] = []
  let recommended: string | null = null
  const head = (v: unknown) => typeof v === 'string' ? OPTION_HEAD.exec(v) : null
  if (rows[i]?.field === 'recommended') {
    const m = head(rows[i].after)
    if (!m) return null
    recommended = m[2]
    i++
  }
  if (rows[i]?.field === 'an answer in your own words is allowed') { if (rows[i].after !== 'yes') return null; i++ }
  while (i < rows.length && rows[i].field.startsWith('option ')) {
    const m = new RegExp(`^option (${OPTION_KEY})$`).exec(rows[i].field)
    const h = head(rows[i].after)
    if (!m || !h || h[2] !== m[1] || h[1] !== String.fromCharCode(65 + keys.length) || keys.includes(m[1]) || keys.length >= 6) return null
    const key = m[1]
    keys.push(key)
    i++
    const pre = `option ${key} `
    let stage = 0 // title(0) text(1) facts(2) cost(3): in this order, each at most once (facts: numbered 1, 2, ...)
    let facts = 0
    if (rows[i]?.field !== `${pre}title`) return null
    i++
    stage = 1
    while (i < rows.length && rows[i].field.startsWith(pre)) {
      const name = rows[i].field.slice(pre.length)
      if (typeof rows[i].after !== 'string') return null
      if (name === 'text' && stage <= 1) stage = 2
      else if (/^fact [1-9][0-9]*$/.test(name) && stage <= 2 && name === `fact ${facts + 1}`) { stage = 2; facts++ }
      else if (name === 'cost' && stage <= 2) stage = 4
      else return null
      i++
    }
  }
  if (rows[i]?.field !== 'addressed to (per host)') return null
  i++
  if (rows[i]?.field !== 'blocks the ticket (per host)') return null
  i++
  if (rows[i]?.field !== 'question hash' || i !== rows.length - 1) return null
  if (recommended !== null && !keys.includes(recommended)) return null
  return { keys, recommended }
}

/**
 * The compact answer review, by the frozen signer View shape (design-question-options.md section 4), enforced HERE and
 * nowhere else. Null (the caller shows the full review, every bound byte) on ANY other shape: an unknown, missing, extra,
 * repeated or misplaced section or row id. Sections, in order: `selected` (choice) | `answer` (Other or a text question) |
 * `note` (only after `selected`), then `question`, `details`, `why`. A choice names an option row it selected, Other is
 * "Something else" with an `answer`, a text question has `answer` and no `selected`, `note` or option rows. The View's
 * `question hash` (the last row, exactly one) is matched with the card's hash by the caller.
 */
export function compactAnswer(content: Content | null | undefined) {
  if (!content || content.summary || !content.ticket || content.ticket.new === true) return null
  if (viewQuestionHash(content) === null) return null
  const secs = content.sections ?? []
  const order = ['selected', 'answer', 'note', 'question', 'details', 'why']
  let last = -1
  for (const s of secs) { // known ids, in the frozen order, none twice
    const at = order.indexOf(s.id)
    if (at <= last || typeof s.text !== 'string') return null
    last = at
  }
  const sec = (id: string) => secs.find(s => s.id === id)
  const selected = sec('selected'), answer = sec('answer'), note = sec('note'), question = sec('question')
  if (!question || (!selected && !answer) || (answer && note)) return null
  if (note && !selected) return null
  const rows = frozenRows(content.fields_changed ?? [])
  if (!rows) return null
  if (selected && answer) { // Other
    if (selected.text !== 'Something else' || rows.keys.length < 2) return null
  } else if (selected) { // a choice: the first line names an option row
    const m = OPTION_HEAD.exec(selected.text.split('\n', 1)[0])
    if (!m || rows.keys.indexOf(m[2]) !== m[1].charCodeAt(0) - 65) return null
  } else if (rows.keys.length > 0 || rows.recommended !== null) return null // a text question
  return { ticket: content.ticket, selected, answer, note, question, details: sec('details') }
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
function QuickSigned({ typed, also, members }: { typed: QuickTyped; also: AlsoSigned; members: Member[] }) {
  return <>
    <section data-testid="sign-typed" aria-label="What you sign" className="space-y-2">
      <h3 className="text-[13px] font-medium text-text">What you sign</h3>
      <dl className="space-y-2">
        <SignedRow label="Title" text={typed.title} />
        {typed.details.trim() && <SignedRow label="Details" text={typed.details.trim()} />}
        {typed.checks.length > 0 && <SignedRow label={typed.checks.length === 1 ? 'Check' : 'Checks'} text={typed.checks.map((c, i) => `${typed.checks.length > 1 ? `${i + 1}. ` : ''}${c.text}${c.cmd ? ` (checked by ${c.cmd})` : ''}`).join('\n')} />}
      </dl>
    </section>
    <p data-testid="sign-also-sentence" className="text-[13px] text-text">This also approves its requirements and plan, made from these checks.</p>
    <details data-testid="sign-also" className="rounded-md border border-border p-3">
      <summary className="cursor-pointer text-[12px] font-medium text-text-muted">Also signed ({also.rows.length})</summary>
      <dl className="mt-2 space-y-2">{also.rows.map((r, i) => <SignedRow key={i} label={r.label} text={r.text} members={r.person ? members : undefined} />)}</dl>
    </details>
  </>
}

export function SignContent({ content, ws, technical, compact = false, quick }: { content: Content; ws?: string; technical?: ReactNode; compact?: boolean; /** A quick task: the typed values, for the folded "Also signed" view. */ quick?: QuickTyped }) {
  const members = useWorkspaceMembers(ws)
  // Every signed person id appears in full somewhere in the view: the plain view shows it short, this list in full.
  const ids = [...new Set((content.fields_changed ?? []).filter(f => !verbatimRow(f.field)).flatMap(f => [f.before, f.after]).filter((v): v is string => personLabel(v, members)?.full != null))]
  const cancels = cancellationRows(content.cancels)
  const summary = content.summary
  // A request on an EXISTING ticket keeps its key (the host's word) and the end of the signed id in the main view; a creation
  // (it signs the key and the title) has them under Technical details only.
  const ticketLine = content.ticket && content.ticket.new !== true
  // Compact: the sections the strip draws itself are not repeated in the expander.
  const compactShown = compact && compactAnswer(content) !== null && !!content.ticket && content.ticket.new !== true
  const also = quick && !content.summary ? alsoSigned(content, quick) : null
  // core sets fields_title 'Applies under' only on a gate review (signcontent.py _gate): the structural mark, not the headline wording.
  const gate = content.fields_title === 'Applies under' && !!content.sections?.length
  const gateSections = content.sections?.filter(s => !CORE_SECTIONS.includes(s.id)) ?? []
  const otherSections = content.sections?.filter(s => CORE_SECTIONS.includes(s.id)).map(s => <section key={s.id}><h3 className="mb-2 text-[13px] font-medium text-text"><Inline>{s.title}</Inline></h3><Prose className="font-sans leading-relaxed text-text">{s.text}</Prose></section>)
  const acceptanceEl = !!content.acceptance?.length && <section><h3 className="mb-2 text-[13px] font-medium text-text">{gate ? 'How we know it is done' : 'Acceptance criteria'}</h3><ul className="space-y-2 leading-relaxed">{content.acceptance.map(a => <li key={a.id}><span aria-label={a.checked ? 'Checked' : 'Unchecked'}>{a.checked ? '☑' : '☐'}</span> <Inline>{a.text}</Inline> <span className="text-[12px] text-text-muted [&_bdi]:text-text-muted"><Raw>{a.id}</Raw></span>{a.manual && <p className="text-[12px] text-text-muted"><Inline>{a.manual}</Inline></p>}</li>)}</ul></section>
  const tasksEl = !!content.tasks?.length && <section><h3 className="mb-2 text-[13px] font-medium text-text">{gate ? 'Steps' : 'Plan tasks'}</h3><ol className="list-decimal space-y-3 pl-5 leading-relaxed">{content.tasks.map(t => <li key={t.n} value={t.n}><Inline>{t.title}</Inline> <span className="text-[12px] text-text-muted [&_bdi]:text-text-muted"><Raw>{t.id}</Raw></span>{t.verify?.map((v,i) => <p key={i} className="mt-1 font-mono text-[12px] leading-relaxed text-text-muted [&_bdi]:text-text-muted">Verify: <Value>{v}</Value></p>)}</li>)}</ol></section>
  const rest = <>
    {also && quick && <QuickSigned typed={quick} also={also} members={members} />}
    {ticketLine && content.ticket && <p data-testid="sign-ticket" className="text-[12px] text-text-muted"><Raw>{content.ticket.key}</Raw> (key per host) · signed id …<Raw>{content.ticket.uid_tail}</Raw></p>}
    {gate ? <GateText sections={gateSections} approving={/^Approve /.test(content.headline ?? '')} acceptance={acceptanceEl} tasks={tasksEl} others={otherSections} /> : null}
    {!gate && !also && content.sections?.filter(s => !(compactShown && COMPACT_IDS.includes(s.id))).map(s => <section key={s.id}><h3 className="mb-2 text-[13px] font-medium text-text"><Inline>{s.title}</Inline></h3><Prose className="font-sans leading-relaxed text-text">{s.text}</Prose></section>)}
    <Pictures images={content.images} />
    {!gate && !also && acceptanceEl}
    {!gate && !also && tasksEl}
    {!also && !!content.fields_changed?.length && <section><h3 className="mb-2 text-[13px] font-medium text-text"><Inline>{content.fields_title ?? 'Changed fields'}</Inline></h3><dl className="space-y-3">{content.fields_changed.map(f => <div key={f.field} className="grid min-w-0 grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-[7rem_minmax(0,1fr)]"><dt className="text-text-muted"><Inline>{`${cap(f.field)}:`}</Inline></dt> <dd className="min-w-0 text-text [&_bdi]:font-sans [&_bdi]:text-[13px]">{(content.fields_title || /: (add|remove)$/.test(f.field) || / \(unchanged, per host\)$/.test(f.field)) && f.before == null ? null : <><Shown plain verbatim={verbatimRow(f.field)} value={f.before} members={members} />{f.before_note && <span className="text-text-muted"> (<Raw>{f.before_note}</Raw>)</span>} → </>}<Shown plain verbatim={verbatimRow(f.field)} value={f.field === 'type' && f.after ? (TYPE_WORDS[f.after] ?? f.after) : f.after} members={members} empty="(not set)" /></dd></div>)}</dl></section>}
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
  // Compact (an answer): the ticket (key and signed id end), the question, the chosen option and the typed note stay in
  // view without opening anything; the other options and the host's facts are in full under Details. If any expected
  // field is missing, the full review is shown: a compact view never hides content.
  const parts = compact ? compactAnswer(content) : null
  if (parts && ticketLine) {
    const { selected, answer, note, question, details, ticket: tk } = parts
    // Order matters: the ticket, then what the person sends in a box of its own (the selected option, or their own words),
    // UNCLAMPED, BEFORE any text someone else wrote (a question that ends in "Your answer: ..." cannot pass for the real
    // thing, and cannot push it off screen). Question and details are clamped here with "Show all".
    return <div data-testid="sign-compact" className="min-w-0 space-y-3 font-sans text-[13px] leading-relaxed text-text [overflow-wrap:anywhere]">
      <p data-testid="sign-ticket" className="text-[12px] text-text-muted"><Raw>{tk.key}</Raw> (key per host) · signed id …<Raw>{tk.uid_tail}</Raw></p>
      {selected && <div data-testid="sign-choice" className="rounded-md border border-brand bg-brand-soft px-3 py-2"><span className="text-[12px] text-text-muted"><Inline>{selected.title}</Inline></span><Prose className="font-sans font-medium leading-relaxed text-text">{selected.text}</Prose></div>}
      {answer && <div data-testid="sign-answer" className="rounded-md border border-brand bg-brand-soft px-3 py-2"><span className="text-[12px] text-text-muted"><Inline>{answer.title}</Inline></span><Prose className="font-sans font-medium leading-relaxed text-text">{answer.text}</Prose></div>}
      {note && <section data-testid="sign-note"><h3 className="mb-1 text-[12px] font-medium text-text-muted"><Inline>{note.title}</Inline></h3><Prose className="font-sans leading-relaxed text-text">{note.text}</Prose></section>}
      <section aria-label={question.title}><h3 className="mb-1 text-[12px] font-medium text-text-muted"><Inline>{question.title}</Inline></h3><ClampedProse>{question.text}</ClampedProse></section>
      {details && <section data-testid="sign-details" aria-label={details.title}><h3 className="mb-1 text-[12px] font-medium text-text-muted"><Inline>{details.title}</Inline></h3><ClampedProse>{details.text}</ClampedProse></section>}
      <details data-testid="sign-exact" className="space-y-3 rounded-md border border-border p-3">
        <summary className="cursor-pointer text-[12px] font-medium text-text-muted">Details: all options and facts</summary>
        <div className="mt-2 max-h-[38vh] min-w-0 space-y-5 overflow-auto pr-3">{rest}</div>
      </details>
    </div>
  }
  // The popover or sheet scrolls as a whole above its fixed Cancel row: no second scroll area inside the review.
  return <div className={`min-w-0 space-y-5 font-sans text-[13px] leading-relaxed text-text [overflow-wrap:anywhere]`}>
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
