// Carry-over between checklist runs: a step the owner passed in an earlier run is shown as done (with the date) and
// counted, unless its text changed since (then it must be tested again). Failed or unfinished steps show their old note.
import type { PreviousRun, StepNotes, StepStatus } from '@/api/testrun'
import type { ChecklistStep } from './run2'

/** A short fingerprint of what a tester reads and does in a step (not a security hash: only "did the text change"). */
export function stepHash(step: ChecklistStep): string {
  const text = JSON.stringify([step.title, step.text ?? '', step.points ?? [], step.commands ?? [], step.see ?? '', step.warn ?? ''])
  let a = 0xdeadbeef
  let b = 0x41c6ce57
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i)
    a = Math.imul(a ^ c, 2654435761)
    b = Math.imul(b ^ c, 1597334677)
  }
  a = Math.imul(a ^ (a >>> 16), 2246822507) ^ Math.imul(b ^ (b >>> 13), 3266489909)
  b = Math.imul(b ^ (b >>> 16), 2246822507) ^ Math.imul(a ^ (a >>> 13), 3266489909)
  return (b >>> 0).toString(16).padStart(8, '0') + (a >>> 0).toString(16).padStart(8, '0')
}

export type Carry =
  | { kind: 'done'; run: string; date: string | null }
  | { kind: 'changed'; run: string }
  | { kind: 'context'; run: string; status: StepStatus; note: string }

const runLabel = (run: string) => run.replace(/^run/, 'run ')
export { runLabel }

/** The newest earlier run that has anything on this step (a mark other than "not yet", or a note). */
function latest(step: ChecklistStep, previous: PreviousRun[]) {
  for (const r of [...previous].sort((x, y) => Number(y.run.slice(3)) - Number(x.run.slice(3)))) {
    const row = r.steps[step.id]
    if (row && (row.status !== 'todo' || row.note.trim() !== '')) return { run: r.run, row }
  }
  return null
}

/** What an earlier run says about this step for the page of the current run, or null for nothing to show. */
export function carryFor(step: ChecklistStep, mine: StepNotes | undefined, previous: PreviousRun[]): Carry | null {
  const hit = latest(step, previous)
  if (!hit) return null
  const { run, row } = hit
  const touched = !!mine && (mine.status !== 'todo' || mine.note.trim() !== '' || mine.images.length > 0)
  if (row.status === 'pass') {
    if (touched || mine?.retest) return null
    // A pass saved before fingerprints existed cannot be compared: it carries over. Only a stored, different one is a change.
    if (!row.hash || row.hash === stepHash(step)) return { kind: 'done', run, date: row.passed_at ?? row.updated }
    return { kind: 'changed', run }
  }
  if (mine && mine.status !== 'todo') return null
  return { kind: 'context', run, status: row.status, note: row.note }
}

/** Local day, e.g. "19 Oct 2026". */
export function dayText(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })
}
