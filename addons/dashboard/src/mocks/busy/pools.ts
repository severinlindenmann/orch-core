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

export const WHYS = [
  'The two sources differ for a few records and the model must pick one.', 'The choice changes the schema, so it has to be settled before the tests are written.',
  'A wrong guess means a backfill later.', 'Finance reads this number in the monthly close.',
]

/**
 * Questions agents ask on the busy day. Each one comes with the options that answer it, what each option costs and
 * which one the agent recommends (the first), so a person can judge them without guessing.
 */
export interface PoolQuestion {
  text: string
  whys: string[]
  options: { label: string; cost: string }[]
}
export const QUESTIONS: PoolQuestion[] = [
  {
    text: 'Which system is the source of truth for meter readings?',
    whys: ['The two sources differ for a few records and the model must pick one.', 'Finance reads this number in the monthly close.'],
    options: [{ label: 'Meter head-end', cost: 'no change to billing' }, { label: 'Billing system', cost: '+1 day to remap' }],
  },
  {
    text: 'Should `valid_from` be a date or a timestamp?',
    whys: ['The choice changes the schema, so it has to be settled before the tests are written.', 'Tariffs change at midnight local time, but readings join on UTC.'],
    options: [{ label: 'Date (local midnight)', cost: 'simpler joins' }, { label: 'Timestamp (UTC)', cost: '+2 tests, one cast' }],
  },
  {
    text: 'Is a one-day delay acceptable for the finance export?',
    whys: ['Finance reads this number in the monthly close.', 'Same-day needs a second run after the late files arrive.'],
    options: [{ label: 'Yes, next day is fine', cost: 'no extra run' }, { label: 'No, same day', cost: '+1 nightly run' }],
  },
  {
    text: 'Do we keep the contract history or replace it?',
    whys: ['A wrong guess means a backfill later.', 'Support looks up old contracts when a customer disputes a bill.'],
    options: [{ label: 'Keep history', cost: '+1 snapshot table' }, { label: 'Replace', cost: 'no backfill, history lost' }],
  },
  {
    text: 'Which tolerance should the reconciliation check use?',
    whys: ['Finance reads this number in the monthly close.', 'Too tight fails on rounding, too loose hides real gaps.'],
    options: [{ label: '0.1 %', cost: 'may fail on rounding' }, { label: '1 %', cost: 'hides small gaps' }],
  },
  {
    text: 'Who owns the fix on the finance side?',
    whys: ['The export changes the numbers finance closes the month with.', 'Someone has to sign off the reconciled totals.'],
    options: [{ label: 'Mara', cost: 'knows the export' }, { label: 'Severin', cost: 'free from Monday' }],
  },
  {
    text: 'May we drop the old `tariff_code` column after the migration?',
    whys: ['The choice changes the schema, so it has to be settled before the tests are written.', 'Two reports still read the old column.'],
    options: [{ label: 'Keep it one release', cost: 'one release of cleanup later' }, { label: 'Drop it now', cost: '2 reports to update first' }],
  },
  {
    text: 'Is VAT rounded per line or per invoice?',
    whys: ['Finance reads this number in the monthly close.', 'The two give different totals by a few cents.'],
    options: [{ label: 'Per line', cost: '+2 tests' }, { label: 'Per invoice', cost: 'matches the PDF' }],
  },
  {
    text: 'Do estimated readings count as readings in the usage report?',
    whys: ['A wrong guess means a backfill later.', 'About 4 % of September readings are estimates.'],
    options: [{ label: 'Yes, flagged as estimated', cost: '+1 column' }, { label: 'No, leave them out', cost: 'usage looks lower' }],
  },
  {
    text: "Which time zone is the customer's bill in?",
    whys: ['Daylight saving days have 23 and 25 hours.', 'The choice changes the schema, so it has to be settled before the tests are written.'],
    options: [{ label: 'Europe/Zurich', cost: 'matches the bill' }, { label: 'UTC', cost: 'shifts one hour twice a year' }],
  },
  {
    text: 'Should the nightly job fail or warn on a missing meter file?',
    whys: ['A missing file today means a gap in tomorrow\'s bills.', 'A wrong guess means a backfill later.'],
    options: [{ label: 'Fail the run', cost: 'nothing ships until fixed' }, { label: 'Warn and continue', cost: 'gap filled next night' }],
  },
]

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
