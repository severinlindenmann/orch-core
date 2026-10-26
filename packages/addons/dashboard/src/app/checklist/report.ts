import type { StepNotes, StepStatus } from '@/api/testrun'
import { ALL_STEPS, RUN2_TITLE } from './run2'
import { dayText, runLabel, type Carry } from './carry'

export const STATUS_LABEL: Record<StepStatus, string> = {
  todo: 'Not yet',
  pass: 'Pass',
  fail_blocking: 'Failed, blocking',
  fail_non_blocking: 'Failed, not blocking',
  skipped: 'Skipped',
}

export const emptyNotes = (): StepNotes => ({ status: 'todo', note: '', images: [] })

/** A step the tester touched: marked, noted, or with a picture. */
export const hasContent = (n: StepNotes | undefined): boolean => !!n && (n.status !== 'todo' || n.note.trim() !== '' || n.images.length > 0)
export const isFailure = (n: StepNotes | undefined): boolean => n?.status === 'fail_blocking' || n?.status === 'fail_non_blocking'
/** What "only steps with notes or failures" keeps. */
export const hasNoteOrFailure = (n: StepNotes | undefined): boolean => !!n && (isFailure(n) || n.note.trim() !== '' || n.images.length > 0)

/** The markdown report of every step that has a mark, a note or a picture, in checklist order. */
export function buildReport(notes: Record<string, StepNotes>, imagesPath: string, savedIn: string, carries: Record<string, Carry> = {}): string {
  const lines = [`# ${RUN2_TITLE}`, '', `Notes saved in: ${savedIn}`, '']
  const touched = ALL_STEPS.filter((s) => hasContent(notes[s.id]))
  const done = (id: string) => carries[id]?.kind === 'done'
  const counts = (Object.keys(STATUS_LABEL) as StepStatus[]).map((k) => `${STATUS_LABEL[k]}: ${ALL_STEPS.filter((s) => (done(s.id) ? 'pass' : notes[s.id]?.status ?? 'todo') === k).length}`)
  lines.push(counts.join(' | '), '')
  const carried = ALL_STEPS.filter((s) => done(s.id) && !hasContent(notes[s.id]))
  if (touched.length === 0 && carried.length === 0) lines.push('Nothing marked yet.')
  for (const step of carried) {
    const c = carries[step.id] as Extract<Carry, { kind: 'done' }>
    const when = dayText(c.date)
    lines.push(`## ${step.id} ${step.title}: Pass (done in ${runLabel(c.run)}${when ? `, ${when}` : ''}, not tested again)`, '')
  }
  for (const step of touched) {
    const n = notes[step.id]
    lines.push(`## ${step.id} ${step.title}: ${STATUS_LABEL[n.status]}`)
    if (n.note.trim()) lines.push('', n.note.trim())
    if (n.images.length) lines.push('', 'Screenshots:', ...n.images.map((name) => `- ${imagesPath}/${name}`))
    lines.push('')
  }
  // Notes of steps that are no longer in the list (a step was renamed or removed) are never dropped from the report.
  const known = new Set(ALL_STEPS.map((s) => s.id))
  for (const [id, n] of Object.entries(notes)) {
    if (known.has(id) || !hasContent(n)) continue
    lines.push(`## ${id} (not in the current list): ${STATUS_LABEL[n.status]}`)
    if (n.note.trim()) lines.push('', n.note.trim())
    if (n.images.length) lines.push('', 'Screenshots:', ...n.images.map((name) => `- ${imagesPath}/${name}`))
    lines.push('')
  }
  return lines.join('\n').trimEnd() + '\n'
}
