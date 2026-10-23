// The quick task signs more than the person typed: the host derives the requirements, the plan, the acceptance
// criteria, the tasks and the ticket's type and label from the title, details and checks. The review shows what was
// typed once ("What you sign"), then folds the derived rows under "Also signed (N)". The fold is only a view: every
// row is one click away and named with a count, and it opens by itself when ANY derived row differs from what the
// typed values would give (a host that signs something else must never hide behind a closed fold).
import type { SignContent } from '@/api/signing'

/** What the person typed into the quick form, as the dashboard posts it. */
export interface QuickTyped {
  title: string
  details: string
  checks: { text: string; cmd?: string }[]
}

export interface DerivedRow {
  /** Row label in the review. */
  label: string
  /** The text signed for that row (multi-line allowed). */
  text: string
}

export interface AlsoSigned {
  rows: DerivedRow[]
  /** True when a derived row is not what the typed values give: the fold opens by itself. */
  differs: boolean
}

const norm = (s: string | null | undefined) => (s ?? '').normalize('NFC').replace(/\r\n?/g, '\n').replace(/^\n+|\n+$/g, '').trim()

/** The host's rule (orch.host.seqquick): requirements default to the title, one check defaults to the title, the plan is derived from the checks. */
export function expectedQuick(t: QuickTyped) {
  const title = norm(t.title)
  const checks = (t.checks.length ? t.checks : [{ text: title }]).map((c) => ({ text: norm(c.text), cmd: c.cmd ? norm(c.cmd) : undefined }))
  const plan = ['Quick task: do these steps in order.', ...checks.flatMap((c, i) => [`${i + 1}. ${c.text}`, ...(c.cmd ? [`   Checked by: ${c.cmd}`] : [])])].join('\n')
  return { title, requirements: norm(t.details) || title, plan, checks }
}

/** The derived rows of a quick-task review and whether any differs from what the typed values give. Pure. */
export function alsoSigned(content: SignContent, typed: QuickTyped): AlsoSigned {
  const want = expectedQuick(typed)
  const rows: DerivedRow[] = []
  let differs = false
  const section = (id: string) => content.sections?.find((s) => s.id === id)
  const requirements = section('requirements')
  if (requirements) rows.push({ label: requirements.title, text: requirements.text })
  const plan = section('plan')
  if (plan) rows.push({ label: plan.title, text: plan.text })
  const acceptance = content.acceptance ?? []
  if (acceptance.length) rows.push({ label: 'Acceptance checks', text: acceptance.map((a) => a.text).join('\n') })
  const tasks = content.tasks ?? []
  if (tasks.length) rows.push({ label: 'Tasks', text: tasks.map((t) => t.title + (t.verify?.length ? ` (checked by ${t.verify.join('; ')})` : '')).join('\n') })
  const fields = content.fields_changed ?? []
  for (const f of fields) rows.push({ label: f.field.charAt(0).toUpperCase() + f.field.slice(1), text: f.after ?? '' })
  // Every derived value must be there and equal what the typed values give.
  differs ||= !requirements || norm(requirements.text) !== want.requirements
  differs ||= !plan || norm(plan.text) !== want.plan
  differs ||= acceptance.length !== want.checks.length || acceptance.some((a, i) => norm(a.text) !== want.checks[i]!.text)
  differs ||= tasks.length !== want.checks.length || tasks.some((t, i) => !norm(t.title).startsWith(want.checks[i]!.text) || (want.checks[i]!.cmd ? !t.verify?.some((v) => norm(v).includes(want.checks[i]!.cmd!)) : !!t.verify?.length))
  const field = (name: string) => fields.find((f) => f.field === name)?.after
  differs ||= norm(field('type')).toLowerCase() !== 'chore' || !(norm(field('labels')).split(/[,\s]+/).includes('quick'))
  if (norm(content.ticket?.title) && norm(content.ticket!.title) !== want.title) differs = true
  return { rows, differs }
}
