// Artifacts for the busy day: every kind (screenshot, log, report, link, dataset, build, diagram, receipt, feedback and
// html pages), spread over about 40 tickets; a few have 15 or more, one log has about 2,000 lines, one dataset ~500 rows.
import { sha256Hex } from '@/api/sha256'
import { fnvHex } from '../derive'
import { MODELS, REPOS } from './pools'
import type { Rng } from './rng'
import { actorOf, ENDED } from './roster'
import { iso, type GenEvent } from './timeline'
import type { Built } from './types'

export type Kind = 'screenshot' | 'log' | 'report' | 'link' | 'dataset' | 'build' | 'diagram' | 'receipt' | 'feedback' | 'other'
export const KINDS: Kind[] = ['screenshot', 'log', 'report', 'link', 'dataset', 'build', 'diagram', 'receipt', 'feedback']

interface Made {
  name: string
  kind: Kind
  preview?: string
  url?: string
  label: string
  bytes?: number
}

const pad = (n: number, w = 2) => String(n).padStart(w, '0')

/** A command log of `lines` lines. */
export function logText(rng: Rng, cmd: string, lines: number): string {
  const out = [`$ ${cmd}`]
  for (let i = 1; i < lines - 1; i++) {
    const m = MODELS[i % MODELS.length]
    const r = rng.next()
    out.push(r < 0.04 ? `WARN  ${m}: ${rng.int(2, 90)} rows have no tariff, skipped` : r < 0.06 ? `ERROR ${m}: ${rng.int(1, 9)} rows failed the check` : `${pad(i % 24)}:${pad(i % 60)}:${pad((i * 7) % 60)}  built ${m} in ${rng.int(10, 4000)} ms  ${rng.int(100, 90000)} rows`)
  }
  out.push(`done: ${lines - 2} steps, exit 0`)
  return out.join('\n') + '\n'
}

/** A dataset as CSV with a header and `rows` rows. */
export function csvText(rng: Rng, rows: number): string {
  const out = ['meter_id,tariff_id,read_at_utc,kwh,billed_chf,status']
  for (let i = 0; i < rows; i++) {
    const kwh = rng.int(5, 900) + rng.int(0, 99) / 100
    out.push(`M-${rng.int(10000, 99999)},T-${rng.int(100, 480)},2026-10-0${rng.int(1, 9)}T${pad(rng.int(0, 23))}:00:00Z,${kwh.toFixed(2)},${(kwh * 0.2184).toFixed(2)},${rng.pick(['ok', 'ok', 'ok', 'estimated', 'late'])}`)
  }
  return out.join('\n') + '\n'
}

const reportText = (rng: Rng, title: string) =>
  `# ${title}\n\n${[...Array(rng.int(6, 14))].map((_, i) => `- Check ${i + 1}: ${rng.pick(['matches finance', 'within tolerance', 'differs by 0.1 %', 'no rows lost', 'two tariffs overlap'])}.`).join('\n')}\n\nSummary: ${rng.int(1, 4)} open points.\n`

function make(rng: Rng, kind: Kind, n: number, key: string): Made {
  const m = rng.pick(MODELS)
  switch (kind) {
    case 'screenshot': return { name: `${m}-${n}.png`, kind, label: `Result of ${m} in the warehouse`, bytes: rng.int(30_000, 400_000) }
    case 'log': return { name: `${m}-run-${n}.log`, kind, label: `Run of ${m}`, preview: logText(rng, `dbt build --select ${m}`, rng.int(24, 120)) }
    case 'report': return { name: `report-${n}.md`, kind, label: `Reconciliation notes ${n}`, preview: reportText(rng, `Reconciliation ${n}`) }
    case 'link': return { name: `dbt docs: ${m} ${n}`, kind, label: `Generated docs for ${m}`, url: `https://docs.acme.example/dbt/${m}`, bytes: 0 }
    case 'dataset': return { name: `${m}-sample-${n}.csv`, kind, label: `Sample rows of ${m}`, preview: csvText(rng, rng.int(5, 40)) }
    case 'build': return { name: `build-${1200 + n}.json`, kind, label: 'CI build result', preview: JSON.stringify({ build: 1200 + n, repo: rng.pick(REPOS).gh, passed: rng.int(40, 220), failed: rng.int(0, 2), skipped: rng.int(0, 6), seconds: rng.int(60, 900) }, null, 2) }
    case 'diagram': return { name: `flow-${n}.mmd`, kind, label: `How ${m} is built`, preview: `flowchart LR\n  raw[raw ${m}] --> stg[stg_${m}]\n  stg --> fct[fct_${m}]\n  fct --> bi[(BI export)]\n` }
    case 'receipt': return { name: `T${n}.receipt.json`, kind, label: `Receipt of T${n}`, preview: JSON.stringify({ task: `T${n}`, cmd: `dbt test --select ${m}`, exit: 0, ms: rng.int(800, 60000), commit: fnvHex(key + n, 7), stdout: `Passed ${rng.int(3, 40)} tests` }, null, 2) }
    case 'feedback': return { name: `feedback-${n}.md`, kind, label: 'Notes from the finance review', preview: `# Feedback ${n}\n\n- ${rng.pick(['Rounding looks right.', 'The March total is off by one cent.', 'Please add the VAT split.'])}\n- ${rng.pick(['Ship it.', 'One more run on the September data.'])}\n` }
    case 'other': return { name: `page-${n}.html`, kind, label: 'Clickable page' }
  }
}

/** Adds one artifact.added event to a ticket (inserted in time order). */
export function addArtifact(b: Built, rng: Rng, made: Made, opts: { at?: number; ac?: string; task?: string } = {}): void {
  const first = Date.parse(b.events[Math.min(2, b.events.length - 1)].at)
  const last = Date.parse(b.events[b.events.length - 1].at)
  const at = opts.at ?? Math.floor((first + rng.next() * Math.max(1000, last - first)) / 1000) * 1000
  const ev: GenEvent = {
    type: 'artifact.added',
    actor: b.worker.includes(':') ? b.worker : actorOf(rng.pick(ENDED)),
    at: iso(at),
    name: made.name,
    kind: made.kind,
    bytes: made.bytes ?? made.preview?.length ?? 0,
    sha256: made.kind === 'other' && made.preview ? sha256Hex(made.preview) : fnvHex(made.name + (made.preview ?? made.url ?? ''), 12),
    ...(opts.task ? { task: opts.task } : {}),
    ...(opts.ac ? { ac: opts.ac } : {}),
    label: made.label,
    ...(made.preview ? { preview: made.preview } : {}),
    ...(made.url ? { url: made.url } : {}),
  }
  b.events.push(ev)
  b.events.sort((x, y) => x.at.localeCompare(y.at))
}

/** An html page artifact (one-off widget page); returns its sha256. */
export function addHtml(b: Built, rng: Rng, name: string, preview: string, label: string): string {
  addArtifact(b, rng, { name, kind: 'other', label, preview })
  return sha256Hex(preview)
}

/**
 * Spreads artifacts over the generated DEMO/INT/CLI tickets. Testing tickets get evidence (they wait for a verdict),
 * done ones some, a few busy ones in progress too. Two tickets get 15 or more, one has a log of 2,000 lines, one a
 * dataset of 500 rows. Every kind appears.
 */
export function addArtifacts(rng: Rng, tickets: Built[], opts: { max: number }): void {
  const pool = tickets.filter((b) => b.arch !== 'epic')
  const testing = pool.filter((b) => b.arch.startsWith('test'))
  const done = rng.shuffle(pool.filter((b) => b.arch === 'done'))
  const wip = rng.shuffle(pool.filter((b) => b.arch === 'wip' || b.arch === 'qSev'))
  const chosen = [...testing, ...done.slice(0, Math.max(0, opts.max - testing.length - 7)), ...wip.slice(0, 7)].slice(0, opts.max)
  let kindIdx = 0
  chosen.forEach((b, i) => {
    const count = i === 0 || i === 1 ? rng.int(15, 18) : i < 5 ? rng.int(8, 12) : rng.int(1, 6)
    const used = new Set<string>()
    for (let n = 1; n <= count; n++) {
      let made = make(rng, KINDS[kindIdx++ % KINDS.length], n, b.definition.key)
      if (used.has(made.name)) made = { ...made, name: `${n}-${made.name}` }
      used.add(made.name)
      const acs = b.definition.acceptance
      addArtifact(b, rng, made, { ...(rng.chance(0.6) ? { ac: rng.pick(acs).id } : {}), ...(b.doneTasks.length && rng.chance(0.5) ? { task: rng.pick(b.doneTasks) } : {}) })
    }
  })
  // The big ones: a log of about 2,000 lines and a dataset of about 500 rows.
  if (chosen[0]) addArtifact(chosen[0], rng, { name: 'full-backfill-run.log', kind: 'log', label: 'Whole September backfill, every step', preview: logText(rng, 'dbt build --full-refresh', 2000) })
  if (chosen[1]) addArtifact(chosen[1], rng, { name: 'reconciliation-all-meters.csv', kind: 'dataset', label: 'Every reconciled meter of September', preview: csvText(rng, 500) })
}
