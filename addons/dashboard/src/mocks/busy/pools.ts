// Text pools for the busy-day generator: fictional "Acme energy data" work. Plain, specific wording.
import type { Rng } from './rng'

export const PERSON_NAME: Record<string, string> = { p_sev: 'Severin', p_mara: 'Mara', p_tom: 'Tom' }

/** Label names (14). Two are long on purpose, to test truncation. */
export const LABELS = [
  'dbt', 'tariffs', 'billing', 'ingest', 'meters', 'quality', 'security', 'docs', 'perf', 'finance', 'api', 'dagster',
  'regulatory-reporting-switzerland-2026', 'tariff-reconciliation-edge-cases-q4',
] as const

export const REPOS = [
  { name: 'acme-energy-dbt', gh: 'acme/energy-dbt', short: 'energy-dbt' },
  { name: 'acme-energy-billing-api', gh: 'acme/billing-api', short: 'billing-api' },
  { name: 'acme-energy-ingest', gh: 'acme/ingest', short: 'ingest' },
] as const

export const FEATURE_VERBS = ['Add', 'Expose', 'Build', 'Support', 'Introduce', 'Automate', 'Export', 'Publish', 'Parameterize', 'Cache']
export const BUG_VERBS = ['Fix', 'Stop', 'Repair', 'Correct', 'Handle']
export const CHORE_VERBS = ['Bump', 'Rename', 'Remove', 'Archive', 'Rotate', 'Clean up', 'Document', 'Pin', 'Split', 'Review']
export const SPIKE_VERBS = ['Investigate', 'Compare', 'Evaluate', 'Prototype', 'Benchmark']

export const OBJECTS = [
  'tariff validity windows', 'meter reading timestamps', 'the billing run summary', 'invoice line rounding', 'credit note matching',
  'the monthly reconciliation', 'smart meter ingestion', 'gas meter staging models', 'solar feed-in tariffs', 'heat pump load profiles',
  'grid fee tables', 'VAT handling on invoices', 'the CO2 report', 'the regulatory export', 'freshness checks on sources',
  'the customer move-out flow', 'outage event deduplication', 'the usage forecast job', 'warehouse role grants', 'the nightly dbt run',
  'seed file naming', 'the finance export', 'prepayment balances', 'meter replacement history', 'the dim_customer snapshot',
  'schema drift alerts', 'the tariff API client', 'retry handling in the loader', 'partition pruning on readings', 'the billing preview page',
  'estimated readings', 'the data catalog descriptions', 'time zone handling', 'service credentials', 'the Airflow to Dagster migration',
  'backfill of September readings', 'late-arriving readings', 'the invoice PDF export', 'customer segments', 'the energy mix dashboard',
]
export const CONDITIONS = [
  'a meter is replaced mid-month', 'the tariff changes at midnight', 'a reading arrives twice', 'daylight saving time starts', 'the finance export is late',
  'a customer moves out', 'the API returns 429', 'a file has a byte order mark', 'a table has no rows', 'two tariffs overlap',
  'the run is restarted halfway', 'a reading is negative', 'the warehouse is paused',
]
export const LONG_CLAUSES = [
  'so that billing, finance and customer support read the same numbers after the nightly run',
  'including the edge cases that only show up when a tariff changes inside a billing period',
  'and write down what we learned, with the queries and the sample data we used to check it',
  'across all three repositories, with a migration note for the people who run the jobs by hand',
  'before the quarterly regulatory report is due, with a fallback if the export is late',
]

export const ASKS = [
  'Which source is the source of truth?', 'Should this be a date or a timestamp?', 'Is a one-day delay acceptable here?',
  'Do we keep the history or replace it?', 'Which tolerance should the check use?', 'Who owns the fix on the finance side?',
  'May we drop the old column after the migration?', 'Is rounding per line or per invoice?', 'Do estimated readings count as readings?',
  'Which time zone is the customer\'s bill in?', 'Should the job fail or warn on a missing file?',
]
export const WHYS = [
  'The two sources differ for a few records and the model must pick one.', 'The choice changes the schema, so it has to be settled before the tests are written.',
  'A wrong guess means a backfill later.', 'Finance reads this number in the monthly close.',
]
export const OPTION_PAIRS: [string, string][] = [['Yes', 'No'], ['Keep history', 'Replace'], ['Date', 'Timestamp'], ['Per line', 'Per invoice'], ['Fail', 'Warn']]

export const COMMENTS = [
  'Looks right to me. Going ahead.', 'Can we split this? The plan is bigger than one ticket.', 'Checked against the September data, the numbers match.',
  'Blocked on the finance export, asked Mara.', 'Reproduced on the staging warehouse.', 'This also touches the preview page, noted in the plan.',
  'Ran it twice, same result.', 'Needs a note in the README before it merges.', 'Not urgent, but the Monday run will hit it.',
  'The failing test is flaky, rerun passes.', 'Moved the check to the staging layer.', 'Please look at the last commit first.',
]
export const TASK_VERBS = ['Write the failing test for', 'Implement', 'Backfill', 'Document', 'Run the reconciliation for', 'Review the output of', 'Add a dbt test for', 'Update the model for']
export const TASK_CMDS = ['dbt test --select {m}', 'dbt build --select {m}+', 'uv run pytest tests/{m} -q', 'dbt seed --select {m}', 'ruff check src/{m}', 'make check-{m}']
export const MODELS = ['stg_meter_readings', 'fct_billing', 'dim_meter', 'dim_customer', 'fct_usage', 'seeds_tariffs', 'stg_tariffs', 'fct_invoices', 'fct_outages', 'snap_contracts']
export const AC_TEXTS = [
  'The check passes on the September data', 'No row is lost or duplicated by the change', 'The run time stays under ten minutes', 'The change is covered by a test',
  'The README explains how to run it', 'Finance signs off the reconciled totals', 'Daylight saving days have 23 and 25 hours', 'The old output is kept for one release',
]
export const VERDICT_TEXTS = ['Checked the output, matches.', 'Numbers add up, thanks.', 'Edge cases covered.', 'Fine for the close.']

export const pickTitle = (rng: Rng, type: string): string => {
  const verbs = type === 'bug' ? BUG_VERBS : type === 'chore' ? CHORE_VERBS : type === 'spike' ? SPIKE_VERBS : FEATURE_VERBS
  const verb = rng.pick(verbs)
  const object = rng.pick(OBJECTS)
  if (type === 'bug') return rng.chance(0.6) ? `${verb} ${object} when ${rng.pick(CONDITIONS)}` : `${verb} ${object}`
  if (type === 'spike') return `${verb} options for ${object}`
  return `${verb} ${object}`
}

/** A title of 92 to 118 characters: the plain title plus clauses, cut at a word boundary. */
export const longTitle = (rng: Rng, base: string): string => {
  let t = base
  while (t.length < 96) t += (t === base ? ' ' : ', ') + rng.pick(LONG_CLAUSES)
  if (t.length > 118) t = t.slice(0, 118).replace(/\s+\S*$/, '')
  return t
}
