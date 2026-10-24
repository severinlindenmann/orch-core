// The quick task signs more than the person typed: the host derives the requirements, the plan, the acceptance
// criteria, the tasks and the ticket's type and label from the title, details and checks. The review shows what was
// typed once ("What you sign"), then folds the derived rows under "Also signed (N)". The fold is only a view: every
// row is one click away and named with a count. The fold is allowed ONLY when the whole prepared review equals, exactly
// (deep, no unknown key, no extra item), the review rebuilt from the typed values with core's quick-task derivation. Anything else
// returns null and the caller shows the full review: it fails closed, a host that signs something else never hides in a fold.
import type { SignContent } from '@/api/signing'

/** What the person typed into the quick form, as the dashboard posts it. */
export interface QuickTyped {
  title: string
  details: string
  checks: { text: string; cmd?: string }[]
  /** The signer's own person id: the owner row of the review must be exactly this person. */
  signer?: string | null
}

export interface DerivedRow {
  /** Row label in the review. */
  label: string
  /** The text signed for that row (multi-line allowed). */
  text: string
  /** The text is a person id: shown as the name with the short id. */
  person?: boolean
}

const norm = (s: string | null | undefined) => (s ?? '').normalize('NFC').replace(/\r\n?/g, '\n').replace(/^\n+|\n+$/g, '').trim()

/** The host's rule (orch.host.seqquick): requirements default to the title, one check defaults to the title, the plan is derived from the checks. */
export function expectedQuick(t: QuickTyped) {
  const title = norm(t.title)
  const checks = (t.checks.length ? t.checks : [{ text: title }]).map((c: { text: string; cmd?: string }) => ({ text: norm(c.text), cmd: c.cmd ? norm(c.cmd) : undefined }))
  const plan = ['Quick task: do these steps in order.', ...checks.flatMap((c, i) => [`${i + 1}. ${c.text}`, ...(c.cmd ? [`   Checked by: ${c.cmd}`] : [])])].join('\n')
  return { title, requirements: norm(t.details) || title, plan, checks }
}

export interface AlsoSigned {
  rows: DerivedRow[]
}

const same = (a: unknown, b: unknown): boolean => JSON.stringify(a) === JSON.stringify(b)
/** The object has exactly these keys (an unknown key is a value this view would not show). */
const keysAre = (o: object, keys: string[]) => same(Object.keys(o).filter((k) => (o as Record<string, unknown>)[k] !== undefined).sort(), [...keys].sort())

/**
 * The derived rows when the prepared review is exactly the one core derives from the typed values; null otherwise
 * (extra or missing section, item, field, label or key; any text that is not exactly the derived text). Pure.
 */
export function alsoSigned(content: SignContent, typed: QuickTyped): AlsoSigned | null {
  const signer = typed.signer
  if (!signer) return null // the owner row must be the signer: without knowing who that is, the full review shows
  const want = expectedQuick(typed)
  const allowed = ['headline', 'title', 'subline', 'ticket', 'sections', 'acceptance', 'tasks', 'fields_title', 'fields_changed', 'notes', 'note']
  if (Object.keys(content).some((k) => !allowed.includes(k) && (content as unknown as Record<string, unknown>)[k] !== undefined)) return null
  // The ticket: present, the typed title, and nothing else unexpected.
  const t = content.ticket
  if (!t || t.title !== want.title || !Object.keys(t).every((k) => ['key', 'title', 'uid_tail', 'key_note', 'new'].includes(k))) return null
  const sections = content.sections ?? []
  if (!Array.isArray(sections)) return null
  if (sections.length !== 2 || !sections.every((s) => keysAre(s, ['id', 'title', 'text']))) return null
  const [req, plan] = sections as [typeof sections[number], typeof sections[number]]
  if (req.id !== 'requirements' || req.title !== 'Requirements' || req.text !== want.requirements || plan.id !== 'plan' || plan.title !== 'Plan' || plan.text !== want.plan) return null
  const acceptance = content.acceptance ?? []
  if (!Array.isArray(acceptance)) return null
  if (acceptance.length !== want.checks.length) return null
  for (const [i, a] of acceptance.entries()) {
    if (!Object.keys(a).every((x) => ['id', 'text', 'checked'].includes(x)) || (a.checked !== undefined && a.checked !== false) || a.id !== `AC${i + 1}` || a.text !== want.checks[i]!.text) return null
  }
  const tasks = content.tasks ?? []
  if (!Array.isArray(tasks)) return null
  if (tasks.length !== want.checks.length) return null
  for (const [i, k] of tasks.entries()) {
    const c = want.checks[i]!
    if (!Object.keys(k).every((x) => ['n', 'id', 'title', 'verify'].includes(x)) || k.n !== i + 1 || k.id !== `T${i + 1}` || k.title !== `${c.text} (proves AC${i + 1})`) return null
    if (!same(k.verify ?? [], c.cmd ? [c.cmd] : [])) return null
  }
  const fields = content.fields_changed ?? []
  if (fields.length !== 3 || !fields.every((f) => (f.before ?? null) === null && Object.keys(f).every((x) => ['field', 'before', 'after'].includes(x)))) return null
  const by = (name: string) => fields.filter((f) => f.field === name)
  if (by('type').length !== 1 || by('owner').length !== 1 || by('labels').length !== 1) return null
  if (by('type')[0]!.after !== 'chore' || by('labels')[0]!.after !== 'quick' || by('owner')[0]!.after !== signer) return null
  const rows: DerivedRow[] = [
    { label: 'Requirements', text: req.text },
    { label: 'Plan', text: plan.text },
    { label: 'Acceptance checks', text: acceptance.map((a) => a.text).join('\n') },
    { label: 'Tasks', text: tasks.map((k) => k.title + (k.verify?.length ? ` (checked by ${k.verify.join('; ')})` : '')).join('\n') },
    ...fields.map((f) => ({ label: f.field.charAt(0).toUpperCase() + f.field.slice(1), text: f.field === 'type' ? 'Chore' : f.after!, ...(f.field === 'owner' ? { person: true } : {}) })),
  ]
  return { rows }
}
