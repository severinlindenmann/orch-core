// Widgets for the busy day (format orch.widgets.v1, plugins/orch-core/docs/widgets.md): eight DEMO tickets carry 4 to 10
// blocks each: bars with many bars, long tables, checks with 30 rows, kv, the catalog's newer types (series, stats,
// gates, spark, diff, callout and the proposed timeline and progress), every template, one-off html pages and a few blocks the page
// must refuse. Pins are computed here (template digest, sha256 of the artifact the block names).
import { findTemplate, templateDigest } from '@/api/widgetTemplates'
import { sha256Hex } from '@/api/sha256'
import { CATALOG, pinned } from '@/api/widgetCatalog'
import { addHtml } from './artifacts'
import { MODELS } from './pools'
import type { Rng } from './rng'
import type { Built } from './types'

const fence = (block: Record<string, unknown>) => '```orch\n' + JSON.stringify(block) + '\n```'
const digestOf = (ref: string) => templateDigest(findTemplate(ref)!)

const bars = (rng: Rng, id: string, n: number) => {
  const data: Record<string, number> = {}
  for (let i = 0; i < n; i++) data[`${MODELS[i % MODELS.length]}_${Math.floor(i / MODELS.length) + 1}`] = rng.int(1, 240)
  const first = Object.keys(data)[0]
  return fence({ type: 'bars', id, title: 'Run time by model, seconds', source: 'dbt run_results, last 14 nights', unit: 's', data, highlight: first })
}
const table = (rng: Rng, id: string, rows: number) =>
  fence({
    type: 'table', id, title: 'Reconciled meters', source: 'select * from reconciliation_september',
    columns: ['meter_id', 'tariff', 'billed kWh', 'finance kWh', 'delta', 'status'],
    rows: Array.from({ length: rows }, (_, i) => {
      const kwh = rng.int(100, 900)
      const delta = rng.int(-3, 3)
      return [`M-${10000 + i * 7}`, `T-${rng.int(100, 480)}`, kwh, kwh + delta, delta, delta === 0 ? 'ok' : 'differs']
    }),
  })
const checks = (rng: Rng, id: string, n: number) =>
  fence({
    type: 'checks', id, title: 'Acceptance criteria', source: 'agent-measured, run of 09:55',
    rows: Array.from({ length: n }, (_, i) => ({ ac: `AC${i + 1}`, verdict: rng.pick(['met', 'met', 'met', 'not_met', 'unproven']), evidence: `${rng.pick(MODELS)}: ${rng.int(1, 90)} of ${rng.int(90, 120)} tables checked.` })),
  })
const kv = (rng: Rng, id: string, n: number) => {
  const items: Record<string, string | number | boolean> = {}
  for (let i = 0; i < n; i++) items[`${rng.pick(['Seen in', 'Rows', 'Tables', 'Owner', 'Window', 'Source', 'Run time', 'Failures'])} ${i + 1}`] = rng.pick([rng.int(1, 9000), 'October build', true, 'finance', '2026-10-05'])
  return fence({ type: 'kv', id, title: 'Facts', source: 'incident notes', items })
}
const beforeAfter = (id: string) =>
  fence({ widget: 'before-after@1', sha256: digestOf('before-after@1'), id, title: 'Rule, before and after', caption: 'Drag to compare.', data: { before: { title: 'before', lines: ['select * from meters'] }, after: { title: 'after', lines: ['select distinct on (meter_id) *', 'order by valid_from desc'] } } })
const lineChart = (rng: Rng, id: string) =>
  fence({ widget: 'line-chart@1', sha256: digestOf('line-chart@1'), id, title: 'Duplicates per daily build', source: 'build logs', data: { title: 'Duplicates per daily build', unit: 'ids', points: Array.from({ length: 12 }, (_, i) => [i + 1, rng.int(0, 120)]) } })
const options = (id: string) =>
  fence({
    widget: 'option-prototype@1', sha256: digestOf('option-prototype@1'), id, title: 'Which record wins?',
    data: { pick: 'latest', options: [{ id: 'latest', title: 'Latest valid_from', cost: 'low', risk: 'low', notes: 'Keeps the newest record.' }, { id: 'crm', title: 'CRM record', cost: 'low', risk: 'medium', notes: 'Trusts the CRM.' }, { id: 'manual', title: 'Decide by hand', cost: 'high', risk: 'low', notes: 'Slow, but exact.' }] },
  })
/** A catalog example under another id (pinned to the template's current digest when it is a template). */
const fromCatalog = (ref: string, id: string) => fence(pinned({ ...CATALOG.find((c) => c.ref === ref)!.example, id }))
const series = (rng: Rng, id: string) => {
  const days = Array.from({ length: 14 }, (_, i) => new Date(Date.UTC(2026, 8, 26 + i)).toISOString().slice(5, 10)) // 09-26 … 10-09
  return fence({ type: 'series', id, title: 'Build minutes per night', source: 'CI history', unit: 'min', x: days, series: [{ name: 'build', values: days.map(() => rng.int(8, 30)) }, { name: 'tests', values: days.map((_, i) => (i === 6 ? null : rng.int(3, 12))) }], markers: [{ x: days[9], label: 'cache on' }] })
}
const stats = (rng: Rng, id: string) =>
  fence({ type: 'stats', id, title: 'Run so far', source: 'agent-measured', items: [{ label: 'Rows checked', value: rng.int(1000, 90000), delta: rng.int(-500, 4000), role: 'ok' }, { label: 'Failures', value: rng.int(0, 40), delta: rng.int(-10, 5), role: 'warn' }, { label: 'Run time, min', value: rng.int(2, 40) }] })
const progress = (rng: Rng, id: string) => {
  const done = rng.int(1, 6)
  const blocked = rng.int(0, 3)
  return fence({ type: 'progress', id, title: 'Tasks', max: done + blocked + 2, segments: [{ label: 'Done', value: done, status: 'ok' }, { label: 'Blocked', value: blocked, status: 'warn' }, { label: 'Open', value: 2, status: 'neu' }] })
}

const PAGE = (n: number) =>
  `<style>body{font:12px/1.5 ui-sans-serif,system-ui,sans-serif}td,th{border-bottom:1px solid GrayText;padding:3px 8px}</style><h4 style="margin:0 0 6px">Prototype ${n}</h4><table><tr><th>meter</th><th>delta</th></tr><tr><td>M-88213</td><td>2</td></tr><tr><td>M-90102</td><td>0</td></tr></table>`

/** What a ticket's blocks are: a list of block texts, widest variety first. */
function blocksFor(b: Built, rng: Rng, variant: number): { section: 'context' | 'verification' | 'current_state' | 'requirements'; text: string }[] {
  const key = b.definition.key
  const html = (n: number, wrongPin = false) => {
    const preview = PAGE(n)
    const name = `proto-${n}.html`
    const pin = addHtml(b, rng, name, preview, 'Clickable prototype')
    return fence({ html: `artifact:${name}`, sha256: wrongPin ? sha256Hex(preview + ' (older version)') : pin, id: `page-${n}`, title: `Prototype ${n}`, source: name, height: 220 })
  }
  const core = (v: number) => [
    { section: 'context' as const, text: bars(rng, `bars-${v}`, 38) },
    { section: 'context' as const, text: kv(rng, `facts-${v}`, 18) },
    { section: 'verification' as const, text: table(rng, `table-${v}`, 70) },
    { section: 'verification' as const, text: checks(rng, `checks-${v}`, 30) },
  ]
  switch (variant) {
    case 0: // ten blocks, every layer
      return [...core(0), { section: 'current_state', text: beforeAfter('diff-0') }, { section: 'current_state', text: lineChart(rng, 'line-0') }, { section: 'current_state', text: options('options-0') }, { section: 'verification', text: html(1) }, { section: 'context', text: html(2) }, { section: 'verification', text: bars(rng, 'bars-small', 5) }]
    case 1: // with a page whose pin no longer matches, and a negative bar
      return [...core(1), { section: 'verification', text: html(3, true) }, { section: 'current_state', text: fence({ type: 'bars', id: 'neg', title: 'Delta by day', data: { mon: 4, tue: -2, wed: 3 } }) }, { section: 'current_state', text: lineChart(rng, 'line-1') }, { section: 'current_state', text: stats(rng, 'stats-1') }, { section: 'context', text: fromCatalog('timeline', 'timeline-1') }]
    case 2: // two blocks sharing an id (both refused), an unknown type
      return [{ section: 'context', text: bars(rng, 'dup', 12) }, { section: 'verification', text: kv(rng, 'dup', 6) }, { section: 'verification', text: fence({ type: 'heatmap', id: 'heat', title: 'By day' }) }, { section: 'current_state', text: beforeAfter('diff-2') }, { section: 'verification', text: checks(rng, 'checks-2', 8) }, { section: 'current_state', text: fromCatalog('callout', 'callout-2') }, { section: 'verification', text: fromCatalog('gates', 'gates-2') }, { section: 'current_state', text: fromCatalog('diff', 'patch-2') }]
    case 3: // a block in a gated section (refused), plus fine ones
      return [{ section: 'requirements', text: bars(rng, 'req-bars', 4) }, { section: 'context', text: table(rng, 'table-3', 25) }, { section: 'verification', text: checks(rng, 'checks-3', 12) }, { section: 'current_state', text: options('options-3') }, { section: 'context', text: fromCatalog('flow-diagram@1', 'flow-3') }, { section: 'current_state', text: fromCatalog('image-compare@1', 'shots-3') }, { section: 'verification', text: series(rng, 'series-3') }, { section: 'current_state', text: progress(rng, 'progress-3') }, { section: 'context', text: fromCatalog('spark', 'spark-3') }]
    default: {
      const n = rng.int(4, 9)
      const pool = [
        () => bars(rng, `bars-${key.toLowerCase()}`, rng.int(6, 30)),
        () => table(rng, `table-${key.toLowerCase()}`, rng.int(8, 60)),
        () => checks(rng, `checks-${key.toLowerCase()}`, rng.int(5, 30)),
        () => kv(rng, `kv-${key.toLowerCase()}`, rng.int(4, 20)),
        () => beforeAfter(`ba-${key.toLowerCase()}`),
        () => lineChart(rng, `lc-${key.toLowerCase()}`),
        () => options(`op-${key.toLowerCase()}`),
        () => html(4),
        () => series(rng, `series-${key.toLowerCase()}`),
        () => stats(rng, `stats-${key.toLowerCase()}`),
        () => progress(rng, `progress-${key.toLowerCase()}`),
        () => fromCatalog('gates', `gates-${key.toLowerCase()}`),
        () => fromCatalog('table-explorer@1', `tx-${key.toLowerCase()}`),
      ]
      // distinct ids: a suffix per block
      return Array.from({ length: n }, (_, i) => {
        // Start where the ticket number says, so the tickets together use the whole pool.
        const text = pool[(i + Number(key.replace(/\D/g, ''))) % pool.length]().replace(/"id":"([a-z0-9-]+)"/, `"id":"$1-${i}"`)
        return { section: (['context', 'verification', 'current_state'] as const)[i % 3], text }
      })
    }
  }
}

/** Puts blocks into the sections of eight DEMO tickets (the ones with artifacts first). */
export function addWidgets(rng: Rng, demo: Built[]): void {
  const withArtifacts = demo.filter((b) => b.events.some((e) => e.type === 'artifact.added') && b.arch !== 'epic')
  const rest = demo.filter((b) => !withArtifacts.includes(b) && ['done', 'testSev', 'testBoth', 'wip'].includes(b.arch))
  const picked = [...withArtifacts.slice(2, 8), ...rest.slice(0, 2)].slice(0, 8)
  picked.forEach((b, i) => {
    // A handoff replaces Current state, so blocks meant for it go into the last handoff the agent wrote.
    const handoff = [...b.events].reverse().find((e) => e.type === 'handoff.written')
    for (const { section, text } of blocksFor(b, rng, i)) {
      if (section === 'current_state' && handoff) handoff.text = `${String(handoff.text)}\n\n${text}`
      else b.body[section] = [b.body[section], text].filter(Boolean).join('\n\n')
    }
  })
}
