// Simulated live work on DEMO-0043 for the mock host (prototype, display only): an agent tree, jobs in the `orch.job/1`
// shape and a work log. Everything is a function of the seconds since the first read, so every poll answers a later
// picture: timers tick, the pipeline moves a step every ~10 s, images appear one by one, tokens grow, the Codex cache cools.
import type { LiveAccount, LiveAgent, LiveCommand, LiveJob, LiveRequest, LiveSnapshot, Milestone, WorkLog, WorkRow, WorkSession } from '@/api/live'

export const LIVE_TICKET = 'DEMO-0043'
export type LiveAction = { op: 'allow-fixes' } | { op: 'decline-fixes' } | { op: 'ignore-job'; job: string } | { op: 'ask-fix'; job: string }

let t0 = Date.now()
const st = { allowedAt: null as number | null, declined: false, ignored: new Set<string>(), fixAsked: false, fixAt: 0 }

/** Start the simulated clock again (tests, and the first read of a page). */
export function resetLive(now = Date.now()) {
  t0 = now
  st.allowedAt = null; st.declined = false; st.ignored = new Set(); st.fixAsked = false; st.fixAt = 0
}

export function liveAct(ticket: string, action: LiveAction, now = Date.now()) {
  if (ticket !== LIVE_TICKET) return
  const e = (now - t0) / 1000
  if (action.op === 'allow-fixes' && st.allowedAt === null) st.allowedAt = e
  else if (action.op === 'decline-fixes') st.declined = true
  else if (action.op === 'ignore-job') st.ignored.add(action.job)
  else if (action.op === 'ask-fix') { st.fixAsked = true; st.fixAt = e }
}

const iso = (ms: number) => new Date(ms).toISOString()
const hhmm = (ms: number) => new Date(ms).toTimeString().slice(0, 5)

export function thumb(i: number, ready: boolean): string {
  const hues = [12, 200, 42, 280, 150, 330, 95, 230, 18, 170, 260, 55]
  const h = hues[i % 12]
  const shapes = ['<rect x="38" y="40" width="44" height="46" rx="6"/>', '<circle cx="60" cy="62" r="26"/>', '<path d="M40 86 60 34l20 52z"/>', '<rect x="34" y="44" width="52" height="34" rx="17"/>']
  const svg = ready
    ? `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="hsl(${h},55%,62%)"/><stop offset="1" stop-color="hsl(${h + 30},50%,38%)"/></linearGradient></defs><rect width="120" height="120" fill="url(#g)"/><g fill="rgba(255,255,255,.85)">${shapes[i % 4]}</g></svg>`
    : '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120"><rect width="120" height="120" fill="#1c2027"/></svg>'
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`
}

const IMAGE_NAMES = ['t1-curve', 't2-curve', 'night-rate', 'day-rate', 'standing-fee', 'peak-hours', 'solar-credit', 'gas-t1', 'gas-t2', 'green-mix', 'summer', 'winter']
const CHART_TOTAL = IMAGE_NAMES.length

const ms = (id: string, at: string, icon: Milestone['icon'], text: string, extra: Partial<Milestone> = {}): Milestone => ({ id, at, icon, text, ...extra })

const ACCOUNTS: LiveAccount[] = [
  { key: 'claude', label: 'Claude · subscription', pct: 38, reset: 'resets 23:40' },
  { key: 'codex', label: 'Codex · ChatGPT', pct: 61, reset: 'weekly, resets Mon' },
]

export function liveSnapshot(ticket: string, now = Date.now()): LiveSnapshot | null {
  if (ticket !== LIVE_TICKET) return null
  const e = Math.max(0, (now - t0) / 1000)
  const checksDone = e >= 20
  const growth = Math.floor(e / 3)
  const testsDone = e >= 30
  const allowedFor = st.allowedAt === null ? null : e - st.allowedAt
  const fixDone = allowedFor !== null && allowedFor >= 25
  const cleanState = st.declined ? 'done' : allowedFor === null ? 'blocked' : fixDone ? 'done' : 'working'
  const h3On = e >= 30

  const mainMs: Milestone[] = [
    ms('m1', '10:40', 'ask', 'You asked: load the tariff tables as dbt seeds, keep the meter join unchanged.'),
    ms('m2', '10:40', 'helper', 'Asked a helper to find the models that read the tariff tables'),
    ms('m3', '10:41', 'done', 'Helper found 3 models; none touch the meter join', { tone: 'ok' }),
    ms('m4', '10:41', 'edit', 'Added the seed files for both tariffs', { tone: 'ok', right: '2 files', command: 'edit seeds/t1_load.csv (+9 -2)' }),
    ms('m5', '10:42', 'helper', 'Asked a helper to write tests for the seeds'),
    ms('m6', '10:42', 'retry', 'Checks failed once, fixed on the next try', { tone: 'past', right: '2 tries', command: 'dbt build --select tariff_seeds', output: 'FAIL not_null_t1_load_valid_from\nGot 4 results, configured to fail if != 0\n\n(next try after a 1-line fix)' }),
    checksDone
      ? ms('m7', '10:43', 'done', 'All 14 checks passed', { tone: 'ok', right: '3.9 s', command: 'dbt build --select tariff_seeds', output: 'Done. PASS=14 WARN=0 ERROR=0 SKIP=0 TOTAL=14' })
      : ms('m7', '10:43', 'run', 'Running the checks', { tone: 'live', command: 'dbt build --select tariff_seeds', output: 'Running with dbt=1.8\nConcurrency: 4 threads\n1 of 14 OK loaded seed file t1_load' }),
  ]
  if (st.fixAsked) mainMs.push(ms('m8', hhmm(t0 + st.fixAt * 1000), 'helper', 'You asked me to fix the failed data checks', { tone: 'ok' }))
  const mainNow = st.fixAsked ? 'Fixing the failed data checks' : checksDone ? 'Checks passed · writing the summary' : 'Running the checks'

  const h1Ms = [ms('a1', '10:40', 'helper', 'Task: find the models that read the tariff tables'), ms('a2', '10:40', 'search', 'Searched the code', { tone: 'ok', command: 'rg -n "tariff" models seeds', output: 'models/billing/stg_tariffs.sql:4\nmodels/billing/fct_invoice_lines.sql:19\nmodels/marts/rate_card.sql:7' }), ms('a3', '10:41', 'done', 'Found 3 models; none touch the meter join', { tone: 'ok' })]
  const h2Ms = [ms('b1', '10:42', 'helper', 'Task: tests for the new seeds'), ms('b2', '10:42', 'helper', 'Asked a helper to check the meter join is unchanged'), testsDone ? ms('b3', '10:44', 'done', 'Wrote 3 tests for the seeds', { tone: 'ok', command: 'dbt test --select t1_load', output: '3 passed in 2.08s' }) : ms('b3', '10:42', 'edit', 'Writing tests', { tone: 'live', command: 'edit tests/test_seeds.py' })]
  const h3Ms = h3On ? [ms('c1', '10:44', 'run', 'Comparing the meter join before and after', { tone: 'live' })] : [ms('c1', '10:42', 'wait', 'Queued: the session runs 2 helpers at a time')]
  const cleanMs = [ms('d1', '10:41', 'helper', 'orch started this task on Codex for DEMO-0043'), ms('d2', '10:41', 'search', 'Found 7 SQL style warnings in 4 files', { tone: 'ok', command: 'sqlfluff lint models/', output: 'models/billing/stg_tariffs.sql:12:1 LT02 indent\n... 6 more' })]
  if (cleanState === 'blocked') cleanMs.push(ms('d3', '10:41', 'wait', 'Waiting for your OK to fix them', { tone: 'warn', right: 'needs you' }))
  else if (cleanState === 'done' && st.declined) cleanMs.push(ms('d3', hhmm(now), 'done', 'You declined the fixes. Nothing was changed.', { tone: 'ok' }))
  else {
    cleanMs.push(ms('d3', hhmm(t0 + (st.allowedAt ?? 0) * 1000), 'done', 'You allowed the fixes', { tone: 'ok' }))
    cleanMs.push(fixDone ? ms('d4', hhmm(now), 'done', 'Fixed 7 warnings in 4 files, checks pass', { tone: 'ok', command: 'sqlfluff fix models/', output: '7 fixable linting violations found\nAll Finished!' }) : ms('d4', hhmm(now), 'edit', 'Fixing 4 files', { tone: 'live', command: 'sqlfluff fix models/' }))
  }
  const locMs = [ms('e1', '10:39', 'helper', 'orch started this task on the office server (local model)'), ms('e2', '10:39', 'edit', 'Wrote a changelog entry', { tone: 'ok', right: '1 file', command: 'edit CHANGELOG.md (+6)' }), ms('e3', '10:39', 'done', 'Done', { tone: 'ok' })]

  const grow = (read: number, written: number, working: boolean) => ({ read: read + (working ? growth * 2700 : 0), reused: Math.round(read * 0.77), written: written + (working ? growth * 360 : 0) })
  const warm = (tokens: number, missUsd: number, ago: number) => ({ lastActivity: iso(now - ago * 1000), ttl: 300, tokens, missUsd })
  const base = { parent: null as string | null, depth: 0, since: null as string | null, request: null as string | null, terminal: '' }
  const agents: LiveAgent[] = [
    { ...base, id: 'main', kind: 'main', tool: 'claude', name: 'Main agent', state: 'working', stateText: 'Working', now: mainNow, since: checksDone ? null : iso(t0 - 14000 + 0), model: 'Sonnet', account: 'Claude · subscription', accountKey: 'claude',
      tokens: grow(182400, 21300, true), usd: 0.94 + growth * 0.015, space: 46, cache: warm(141000, 0.42, e % 25), milestones: mainMs,
      chat: [{ from: 'you', text: 'Load the tariff tables as dbt seeds. Keep the meter join unchanged.' }, { from: 'agent', text: `${mainNow}...` }], terminal: `> Load the tariff tables as dbt seeds.\n\n* ${mainNow}\n  (esc to interrupt)` },
    { ...base, id: 'h1', kind: 'helper', tool: 'claude', name: 'Explore billing models', parent: 'main', depth: 1, state: 'done', stateText: 'Done', now: 'Found 3 models; none touch the meter join', model: 'Haiku', account: 'Claude · subscription', accountKey: 'claude',
      tokens: { read: 38200, reused: 21000, written: 2900 }, usd: 0.05, space: 18, cache: warm(21000, 0.02, 110 + e), milestones: h1Ms,
      chat: [{ from: 'you', text: 'Find the models that read the tariff tables.' }, { from: 'agent', text: 'Found 3 models; none touch the meter join.' }], terminal: 'Helpers share the main agent terminal.' },
    { ...base, id: 'h2', kind: 'helper', tool: 'claude', name: 'Write seed tests', parent: 'main', depth: 1, state: testsDone ? 'done' : 'working', stateText: testsDone ? 'Done' : 'Working', now: testsDone ? 'Wrote 3 tests for the seeds' : 'Writing tests for the not-null and date checks', since: testsDone ? null : iso(t0 - 5000), model: 'Sonnet', account: 'Claude · subscription', accountKey: 'claude',
      tokens: grow(61500, 8800, !testsDone), usd: 0.31 + (testsDone ? 0 : growth * 0.015), space: 27, cache: warm(40000, 0.12, testsDone ? e - 30 : e % 25), milestones: h2Ms,
      chat: [{ from: 'you', text: 'Write tests for the new seeds.' }, { from: 'agent', text: testsDone ? 'Wrote 3 tests.' : 'Writing tests...' }], terminal: 'Helpers share the main agent terminal.' },
    { ...base, id: 'h3', kind: 'helper', tool: 'claude', name: 'Check the meter join is unchanged', parent: 'h2', depth: 2, state: h3On ? 'working' : 'queued', stateText: h3On ? 'Working' : 'Queued', now: h3On ? 'Comparing the meter join before and after' : 'Starts when a helper slot is free (2 at a time)', since: h3On ? iso(t0 + 30000) : null, model: 'Haiku', account: 'Claude · subscription', accountKey: 'claude',
      tokens: h3On ? { read: 4200 + (e - 30) * 300, reused: 0, written: 300 } : { read: 0, reused: 0, written: 0 }, usd: 0, space: h3On ? 6 : 0, cache: h3On ? warm(4200, 0.01, 0) : null, milestones: h3Ms,
      chat: [{ from: 'you', text: 'Check the meter join is unchanged.' }], terminal: 'Helpers share the main agent terminal.' },
    { ...base, id: 'c1', kind: 'task', tool: 'codex', name: 'Clean up SQL formatting', state: cleanState, stateText: cleanState === 'blocked' ? 'Needs you' : cleanState === 'done' ? 'Done' : 'Working',
      now: cleanState === 'blocked' ? 'Waiting for your OK to fix 4 files' : st.declined ? 'You declined the fixes' : fixDone ? 'Fixed 7 warnings in 4 files' : 'Fixing 4 files', since: cleanState === 'working' ? iso(t0 + (st.allowedAt ?? 0) * 1000) : null,
      model: 'gpt-5.4', account: 'Codex · ChatGPT', accountKey: 'codex', tokens: { read: 24100, reused: 12000, written: 1600 }, usd: 0.07, space: 9,
      cache: allowedFor === null ? warm(12000, 0.03, 250 + e) : warm(12000, 0.03, fixDone ? e - st.allowedAt! - 25 : 0), milestones: cleanMs, request: cleanState === 'blocked' ? 'req-fix' : null,
      chat: [{ from: 'agent', text: cleanState === 'blocked' ? 'I need your OK to fix 4 files.' : 'Working on it.' }], terminal: `$ ${cleanState === 'working' ? 'sqlfluff fix models/' : 'sqlfluff lint models/'}` },
    { ...base, id: 'l1', kind: 'task', tool: 'local', name: 'Draft changelog entry', state: 'done', stateText: 'Done', now: 'Wrote 1 entry', model: 'qwen3-coder', account: 'Local · office server', accountKey: null,
      tokens: { read: 9800, reused: 0, written: 640 }, usd: 0, space: 31, cache: null, milestones: locMs, chat: [{ from: 'agent', text: 'Wrote the changelog entry.' }], terminal: '$ git diff --stat' },
  ]

  // ---- jobs (orch.job/1)
  const stepDone = Math.min(5, 2 + Math.floor(e / 10))
  const steps = ['read_tariff_files', 'validate_schema', 'load_tariff_tables', 'refresh_rate_views', 'publish_to_billing'].map((name, i): LiveJob['steps'][number] =>
    i < stepDone ? { name, state: 'ok', time: ['2:10', '0:41', '2:47', '1:12', '0:20'][i] } : i === stepDone ? { name, state: 'running', time: '' } : { name, state: 'queued', time: '' })
  const pipeDone = stepDone >= 5
  const runIdx = steps.findIndex((s) => s.state === 'running')
  const dbu = (3.1 + Math.floor(e / 4) * 0.2).toFixed(1)
  const imgs = Math.min(CHART_TOTAL, 7 + Math.floor(e / 3))
  const jobs: LiveJob[] = [
    { id: 'j1', parent: 'main', name: 'Nightly tariff load', source: 'Databricks', via: 'shell', cmd: 'dbx-run --job 8812  (databricks jobs run-now)', state: pipeDone ? 'done' : 'working',
      progress: { done: stepDone, total: 5, unit: 'steps' }, steps, metrics: [{ label: 'Rows loaded', value: '1.8M' }, { label: 'Cluster', value: '4 workers' }, { label: 'Cost so far', value: `${dbu} DBU` }],
      link: { label: 'Open in Databricks', url: 'https://example.invalid/databricks/job/8812/run/551' }, images: [], table: null, log: '', since: iso(t0 - 252000),
      now: pipeDone ? 'Done · tariff tables loaded' : `Step ${runIdx + 1} of 5 · ${steps[runIdx].name.replace(/_/g, ' ')}`, summary: pipeDone ? '5 tasks done, 1.8M rows, rate views refreshed' : '', ignored: false, fixAsked: false },
    { id: 'j2', parent: 'h2', name: 'Tariff chart images', source: 'Local image model', via: 'skill', cmd: 'skill local-images · FLUX.1 on the office GPU', state: imgs >= CHART_TOTAL ? 'done' : 'working',
      progress: { done: imgs, total: CHART_TOTAL, unit: 'images' }, steps: [], metrics: [{ label: 'GPU time', value: `${(4.5 + (imgs - 7) * 0.6).toFixed(1)} min` }, { label: 'Model', value: 'FLUX.1 schnell' }, { label: 'Cost', value: 'Runs on your server' }],
      link: null, images: IMAGE_NAMES.map((n, i) => ({ name: `${n}.png`, src: i < imgs ? thumb(i, true) : null })), table: null, log: '', since: iso(t0 - 271000),
      now: imgs >= CHART_TOTAL ? `Done · ${CHART_TOTAL} images` : `${imgs} of ${CHART_TOTAL} images`, summary: imgs >= CHART_TOTAL ? `${CHART_TOTAL} images in out/charts` : '', ignored: false, fixAsked: false },
    { id: 'j3', parent: 'main', name: 'dbt data checks', source: 'dbt', via: 'shell', cmd: 'dbt build --select tariff_seeds+', state: 'failed',
      progress: { done: 48, total: 48, unit: 'checks' }, steps: [], metrics: [{ label: 'Checks', value: '48' }, { label: 'Passed', value: '46' }, { label: 'Failed', value: '2' }], link: null, images: [],
      table: { title: 'Failed checks', cols: ['Check', 'Table', 'Problem'], rows: [['not_null', 'tariff_rates', '4 empty valid_from dates'], ['accepted_values', 'tariff_rates.unit', '"kwh " has a trailing space (12 rows)']] },
      log: 'Completed with 2 errors and 0 warnings:\nFailure in test not_null_tariff_rates_valid_from (seeds/schema.yml)\n  Got 4 results, configured to fail if != 0', since: iso(t0 - 60000),
      now: st.ignored.has('j3') ? 'Ignored for this run' : st.fixAsked ? 'Main agent is fixing it' : '2 of 48 checks failed', summary: '2 of 48 checks failed', ignored: st.ignored.has('j3'), fixAsked: st.fixAsked },
  ]

  const commands: LiveCommand[] = [
    { id: 'k1', agent: 'main', command: 'dbt build --select tariff_seeds', status: checksDone ? 'ok' : 'running', duration: checksDone ? '3.9 s' : '', since: checksDone ? null : iso(t0 - 14000), output: checksDone ? 'Done. PASS=14 WARN=0 ERROR=0 SKIP=0 TOTAL=14' : 'Running with dbt=1.8\n1 of 14 OK loaded seed file t1_load', redacted: false },
    { id: 'k2', agent: 'c1', command: 'sqlfluff fix models/', status: cleanState === 'blocked' ? 'waiting' : cleanState === 'working' ? 'running' : st.declined ? 'waiting' : 'ok', duration: cleanState === 'blocked' ? 'needs you' : st.declined ? 'declined' : fixDone ? '6.2 s' : '', since: cleanState === 'working' ? iso(t0 + (st.allowedAt ?? 0) * 1000) : null,
      output: cleanState === 'blocked' ? 'Not run yet. Waiting for your OK.' : st.declined ? 'Not run. You declined.' : fixDone ? '7 fixable linting violations found\nAll Finished!' : 'Fixing models/billing/stg_tariffs.sql ...', redacted: false },
    { id: 'k3', agent: 'h2', command: 'dbt test --select t1_load', status: 'ok', duration: '2.1 s', since: null, output: '3 passed in 2.08s', redacted: false },
    { id: 'k4', agent: 'main', command: 'dbt build --select tariff_seeds', status: 'failed', duration: '4.8 s · fixed after', since: null, output: 'FAIL not_null_t1_load_valid_from\nGot 4 results, configured to fail if != 0', redacted: false },
    { id: 'k5', agent: 'c1', command: 'sqlfluff lint models/', status: 'ok', duration: '0.9 s · 7 warnings', since: null, output: 'models/billing/stg_tariffs.sql:12:1 LT02 indent\n... 6 more', redacted: false },
    { id: 'k6', agent: 'main', command: 'curl -s -H "Authorization: Bearer ••••" https://api.example.invalid/tariffs/health', status: 'ok', duration: '0.2 s', since: null, output: '{"ok":true}', redacted: true },
    { id: 'k7', agent: 'h1', command: 'rg -n "tariff" models seeds', status: 'ok', duration: '0.1 s', since: null, output: 'models/billing/stg_tariffs.sql:4\nmodels/billing/fct_invoice_lines.sql:19\nmodels/marts/rate_card.sql:7', redacted: false },
    { id: 'k8', agent: 'l1', command: 'git diff --stat', status: 'ok', duration: '0.1 s', since: null, output: ' CHANGELOG.md | 6 ++++++', redacted: false },
  ]

  const requests: LiveRequest[] = cleanState === 'blocked' ? [{
    id: 'req-fix', agent: 'c1', title: 'Allow automatic code fixes?',
    changes: ['Changes formatting and indentation in 4 files under models/.', "Needed so the ticket's checks can pass.", 'Nothing is deployed. You can review every change under Changes before it is merged.'],
    command: 'sqlfluff fix models/', askedBy: 'Codex · ChatGPT account · gpt-5.4',
  }] : []

  const events: Record<string, unknown>[] = [
    { v: 1, kind: 'start', job: 'dbx-551', type: 'pipeline', source: 'Databricks', title: 'Nightly tariff load', total: 5 },
    { v: 1, kind: 'link', job: 'dbx-551', label: 'Open in Databricks', url: 'https://example.invalid/databricks/job/8812/run/551' },
    ...steps.filter((s) => s.state !== 'queued').map((s) => ({ v: 1, kind: 'step', job: 'dbx-551', step: s.name, state: s.state })),
    ...(pipeDone ? [{ v: 1, kind: 'end', job: 'dbx-551', state: 'ok', summary: jobs[0].summary }] : []),
    { v: 1, kind: 'start', job: 'img-7', type: 'images', source: 'Local image model', title: 'Tariff chart images', total: CHART_TOTAL, unit: 'images' },
    ...IMAGE_NAMES.slice(0, imgs).map((n) => ({ v: 1, kind: 'artifact', job: 'img-7', path: `out/charts/${n}.png`, mime: 'image/png', preview: true })),
    { v: 1, kind: 'progress', job: 'img-7', done: imgs, total: CHART_TOTAL, unit: 'images' },
    { v: 1, kind: 'start', job: 'dbt-3', type: 'checks', source: 'dbt', title: 'dbt data checks', total: 48 },
    { v: 1, kind: 'table', job: 'dbt-3', title: 'Failed checks', columns: ['Check', 'Table', 'Problem'], rows: jobs[2].table!.rows },
    { v: 1, kind: 'end', job: 'dbt-3', state: 'failed', summary: '2 of 48 checks failed' },
  ]

  return { ticket, title: 'Load tariff tables as dbt seeds', agents, jobs, commands, requests, accounts: ACCOUNTS, events }
}

// ---- Work log of the ticket (3 days of sessions)
const row = (time: string, icon: WorkRow['icon'], lead: string, text: string, tags: WorkRow['tags'], extra: Partial<WorkRow> = {}): WorkRow => ({ time, icon, lead, text, tags, ...extra })

export function liveWorkLog(ticket: string): WorkLog | null {
  if (ticket !== LIVE_TICKET) return null
  const sessions: WorkSession[] = [
    { id: 's4', day: 'Today', when: '10:38 – now', live: true, who: 'Main agent', tool: 'claude', account: 'Claude · Sonnet', what: 'Loading the tariff tables as dbt seeds', chips: ['2 helpers', '3 jobs', '12 commands'], actions: ['Open focus'], rows: [
      row('10:40', 'helper', 'Helper Explore billing models', 'started (Claude · Haiku)', []),
      row('10:41', 'job', 'Job Nightly tariff load', '· Databricks', ['job'], { status: { tone: 'ok', text: 'done' }, right: '8:12', link: 'Open in Databricks' }),
      row('10:44', 'job', 'Job Tariff chart images', '· local image model', ['job'], { status: { tone: 'ok', text: 'done' }, right: '5:31', thumbs: 12 }),
      row('10:46', 'job', 'Job dbt data checks', '· dbt', ['job', 'bad'], { status: { tone: 'bad', text: '2 of 48 failed' }, right: '0:48', table: [['not_null', 'tariff_rates', '4 empty valid_from dates'], ['accepted_values', 'tariff_rates.unit', '"kwh " trailing space']] }),
      row('10:47', 'you', 'You', 'chose "Ask the main agent to fix" for the failed checks', ['you']),
      row('10:49', 'edit', 'Fixed the unit values', 'in seeds/tariff_rates.csv; re-ran the checks', ['job'], { status: { tone: 'ok', text: '48 of 48' } }),
    ] },
    { id: 's3', day: 'Yesterday', when: '16:20 – 16:58 · 38 min', live: false, who: 'Main agent', tool: 'claude', account: 'Claude · Sonnet', what: 'Planned the seeds and ran a dry load on the test cluster', chips: ['1 helper', '2 jobs', '27 commands', '6 files'], actions: ['Read the chat', 'Changes from this session'], rows: [
      row('16:20', 'ask', 'Started by you:', '"Load the tariff tables as dbt seeds"', ['you']),
      row('16:24', 'helper', 'Helper Explore billing models', '(Claude · Haiku) found 3 models that read the tariffs', []),
      row('16:31', 'job', 'Job Pipeline dry run', '· Databricks', ['job'], { status: { tone: 'ok', text: 'done' }, right: '3:40' }),
      row('16:40', 'you', 'You', 'allowed "write to table billing.tariff_rates" (once)', ['you']),
      row('16:47', 'job', 'Job Rate card preview', '· 1 PDF', ['job'], { right: '0:22', link: 'rate-card-preview.pdf' }),
      row('16:58', 'done', 'Ended:', 'plan step 2 of 3 done; left a note for the next session', []),
    ] },
    { id: 's2', day: 'Yesterday', when: '11:02 – 11:09 · 7 min', live: false, who: 'Codex task', tool: 'codex', account: 'Codex · ChatGPT', what: 'Cleaned up 5 SQL style warnings in the billing models', chips: ['1 job', '3 files'], actions: ['Read the chat'], rows: [
      row('11:03', 'you', 'You', 'allowed automatic code fixes (once)', ['you']),
      row('11:05', 'done', '5 warnings fixed,', 'checks pass', []),
    ] },
    { id: 's1', day: '2 days ago', when: '09:30 – 09:41 · 11 min', live: false, who: 'Main agent', tool: 'claude', account: 'Claude · Sonnet', what: 'Refined the ticket: wrote the plan and 4 acceptance checks', chips: ['plan approved by you'], actions: ['Read the chat'], rows: [
      row('09:41', 'done', 'Plan sent for approval', '-> you approved it at 10:02', ['you']),
    ] },
  ]
  return { ticket, sessions, days: 3, usd: 0.12 }
}
