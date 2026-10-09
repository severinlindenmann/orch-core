// The widget catalog (wave 3, N4): every type core draws and every template the mock ships, each with a working
// example, what it shows and where it is allowed. Pure data, part of the API contract: the widgets addon builds its
// gallery from it, demo tickets and the busy day reuse the examples, and a test parses every example with the same
// strict parser the ticket page uses.
import { findTemplate, templateDigest } from './widgetTemplates'

export interface CatalogEntry {
  kind: 'core' | 'template'
  /** The core type, or the template as name@version. */
  ref: string
  title: string
  shows: string
  allowed: string
  /** The block without its pin; templateBlock adds the current sha256. */
  example: Record<string, unknown>
}

/** Two small inline screenshots (320×180 PNG) for image-compare examples. */
export const SAMPLE_BEFORE_PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAUAAAAC0CAIAAABqhmJGAAACPUlEQVR42u3dsQmAMBBA0UwhNvYWls5gY+1qaVJnT0urgBiQHDx4E4T73ZFL67YDQSVPAAIGBAwIGAQMCBgQMCBgEDAQNOBcKh8YNQQsYBCwgBEwAkbAAgYBCxgBC1jACBgBY5EDEDAIGBAwIGBAwCBgQMCAgEHAQJSAp3kBghIwCBgQMCBgEDAgYEDAgIBBwICAAQGDgAEBAwIGBAwCfus4LwZn3AUsYAEjYASMgBGwgAUsYASsEAEjYATcI8rBdwEjYAELWMACFrCABSxgASNgAQsYAQtYwAIWsIARsIAFjIARsIARsIAFjIARMAJGwAIWsIDxqR0gYEDAIGBAwICAAQGDgAEBAwIGAQMCBgQMCBgEDAgYEDAgYBAwIGBAwCBgQMCAgAEBg4ABAT+cO8ERGQGDgAWMgAUsYAQsYBCwgBGwgAVMLLnUwQkYBCxgBCxgASNgAYOABYyABSxgBCxgASNgAYNFDgEjYAELGAELWMAIWMAgYAEjYAEDAgYEDAIGBAwIGBAwCBgQMCBgEDAgYEDAgIBBwICAAQEDAgYBAwIGBAwCBgQMCBgQMAgYEHAvh0JAwAJGwAIWMAIWsIARsIARsIAFjIAFLOD/5VJDELCAEbCABSxgAQtYwAIWsIAFLGABC1jAAhawgAUsYAELWMACFjAIWMAIWMACRsACFjACFjACFrCAETAgYEDAIGBAwICAAQGDgAEBAwIGAQMCBgQMCBgEDAgYEDAgYBAwIGBAwCBgQMCAgIGmGw1uZLcMMH2CAAAAAElFTkSuQmCC'
export const SAMPLE_AFTER_PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAUAAAAC0CAIAAABqhmJGAAACPElEQVR42u3dsQmAMBBAUacQG3sLwcaBnMrGMriKQ1k5gqKCOfPgTRDud0eu6voRCKryBCBgQMCAgEHAgIABAQMCBgEDQQOel5UbjBoCFjAIWMAIGAEjYAGDgAWMgAUsYASMgLHIAQgYBAwIGBAwIGAQMCBgQMAgYCBKwHXTAkEJGAQMCBgQMAgYEDAgYEDAIGBAwICAQcCAgAEBAwIGAV81bInMGXcBC1jACBgBI2AELGABCxgBK0TACBgBPxHl4LuAEbCABSxgAQtYwAIWsIARsIAFjIAFLGABC1jACFjAAkbACFjACFjAAkbACBgBI2ABC1jA+NQOEDAgYBAwIGBAwICAQcCAgAEBg4ABAQMCBgQMAgYEDAgYEDAIGBAw8GXA07rD6yQqYAQsYAEjYAGDgAWMgBEwAhawgMknYAe+BYyABQwCFjACFrCAEbCABYyABQwCFjACFrCAschhkUPACFjAIGABI2ABCxgBCxgE7FM7QMAgYEDAgIBBwICAAQEDAgYBAwIGBAwIGAQMCBgQMAgYEDAgYEDAIOBzw5aKYnQQsIBBwAJGwAIWMAIWMAhYwCBgAZfGgW8BC1jAAhYwAhawgBGwgAUsYAELWMACFrCAEbCABYyABSxgLHIIGAQsYBCwgBGwgAWMgAUMEQIGBAwIGAQMCBgQMCBgEDAgYEDAIGBAwICAAQGDgAEBAwIGBAwCBgQMCBgEDAgYEDAgYBAwIGBAwICAQcCAgAEBg4ABAQMCBgQMv3MAYnCZGlqwlTsAAAAASUVORK5CYII='

const CORE_ALLOWED = 'Context, Current state and Verification. Refused in Summary, Requirements, Out of scope, Plan and Decisions: a gate signs that text.'
const TEMPLATE_ALLOWED = 'The same sections as core types, and only while the Widgets addon is on in the workspace. Agent HTML runs in a sandboxed frame: no network, no navigation, no access to this dashboard. The block pins the template by sha256.'

export const CATALOG: CatalogEntry[] = [
  { kind: 'core', ref: 'metric', title: 'Metric', shows: 'Headline numbers with unit and change, better or worse.', allowed: CORE_ALLOWED,
    example: { type: 'metric', id: 'ex-metric', title: 'Seed load', source: 'dbt seed, run of 09:55', items: [{ label: 'Tables loaded', value: 31, unit: 'of 40', delta: 9, delta_unit: 'since 09:00', better: 'up' }, { label: 'Seed time', value: 48, unit: 's', delta: -12, better: 'down' }, { label: 'Rows', value: '1.2 M' }] } },
  { kind: 'core', ref: 'line', title: 'Line', shows: 'One to four series over time, with gaps and event markers.', allowed: CORE_ALLOWED,
    example: { type: 'line', id: 'ex-line', title: 'Seed time per night', source: 'dbt run_results', unit: 's', x: ['10-02', '10-03', '10-04', '10-05', '10-06', '10-07', '10-08', '10-09'], series: [{ name: 'seed', values: [61, 64, 60, 72, null, 58, 51, 48] }, { name: 'test', values: [22, 21, 25, 24, 23, 20, 19, 19] }], markers: [{ at: '10-07', label: 'typed columns' }] } },
  { kind: 'core', ref: 'sparkline', title: 'Sparkline', shows: 'A word-sized trend with the latest value.', allowed: CORE_ALLOWED,
    example: { type: 'sparkline', id: 'ex-spark', label: 'CI time, last 12 runs', unit: 'min', values: [9.1, 8.7, 8.9, 8.2, 7.9, 8.4, 7.1, 6.8, 6.9, 6.4, 6.2, 6.1] } },
  { kind: 'core', ref: 'progress', title: 'Progress', shows: 'A value against a maximum, or segments that add up to it.', allowed: CORE_ALLOWED,
    example: { type: 'progress', id: 'ex-progress', title: 'Tasks', max: 4, segments: [{ label: 'Done', value: 1, status: 'ok' }, { label: 'Blocked on Q2', value: 2, status: 'warn' }, { label: 'Not started', value: 1, status: 'neutral' }] } },
  { kind: 'core', ref: 'checks', title: 'Checks', shows: "A verdict per acceptance criterion: the agent's check, not the human verdict.", allowed: 'Verification, where the evidence lives; Context and Current state draw it too. ' + CORE_ALLOWED.split('. ')[1],
    example: { type: 'checks', id: 'ex-checks', title: 'Acceptance criteria', source: 'agent-measured', rows: [{ ac: 'AC1', verdict: 'met', evidence: 'dbt seed loads 40 tables.' }, { ac: 'AC2', verdict: 'unproven', evidence: 'Waits on the valid_from type.' }, { ac: 'AC3', verdict: 'not_met', evidence: 'README has no refresh section.' }] } },
  { kind: 'core', ref: 'status', title: 'Status', shows: 'A list of items, each OK, warning, failed, skipped or running.', allowed: CORE_ALLOWED,
    example: { type: 'status', id: 'ex-status', title: 'Checks on the branch', items: [{ name: 'dbt build', status: 'ok', detail: '4 min 12 s' }, { name: 'sqlfluff', status: 'warn', detail: '2 long lines' }, { name: 'dbt test', status: 'fail', detail: 'unique_meter_id: 112 rows' }, { name: 'docs', status: 'skip' }, { name: 'nightly', status: 'running' }] } },
  { kind: 'core', ref: 'timeline', title: 'Timeline', shows: 'Dated steps: what happened, what happens now, what is next.', allowed: CORE_ALLOWED,
    example: { type: 'timeline', id: 'ex-timeline', title: 'So far', items: [{ at: '2026-10-07', label: 'Plan approved', status: 'done' }, { at: '2026-10-08T14:10Z', label: 'T1 done', status: 'done' }, { at: '2026-10-09T09:55Z', label: 'T2 check fails', status: 'failed', note: 'Windows across midnight drop the whole day.' }, { at: '2026-10-09T11:30Z', label: 'Fix the mask', status: 'current' }, { at: '2026-10-10', label: 'Verdict', status: 'next' }] } },
  { kind: 'core', ref: 'diff', title: 'Diff', shows: 'A few changed lines as a unified diff, at most 200.', allowed: CORE_ALLOWED,
    example: { type: 'diff', id: 'ex-diff', title: 'The mask, before and after', file: 'models/fct_usage_forecast.sql', lines: '@@ -14,3 +14,4 @@\n where reading_at not in (\n-  select day from outages\n+  select generate_series(starts_at, ends_at, interval 1 hour)\n+  from outages\n )' } },
  { kind: 'core', ref: 'callout', title: 'Callout', shows: 'One sentence the reader must not miss: a note, OK, warning, problem or decision.', allowed: CORE_ALLOWED,
    example: { type: 'callout', id: 'ex-callout', role: 'decision', text: 'Mask by hour, not by day: a window that crosses midnight would otherwise drop two whole days.' } },
  { kind: 'core', ref: 'bars', title: 'Bars', shows: 'Horizontal labelled bars, one highlighted.', allowed: CORE_ALLOWED,
    example: { type: 'bars', id: 'ex-bars', title: 'Bundle size, kB', source: 'size.txt', unit: 'kB', data: { main: 412, branch: 286 }, highlight: 'branch' } },
  { kind: 'core', ref: 'table', title: 'Table', shows: 'A table that scrolls inside its own box when long.', allowed: CORE_ALLOWED,
    example: { type: 'table', id: 'ex-table', title: 'Duplicated ids', columns: ['meter_id', 'rows', 'source'], rows: [['M-88213', 2, 'crm'], ['M-90102', 2, 'billing'], ['M-90377', 3, 'crm']] } },
  { kind: 'core', ref: 'kv', title: 'Facts', shows: 'Facts as label and value.', allowed: CORE_ALLOWED,
    example: { type: 'kv', id: 'ex-kv', title: 'Incident facts', items: { 'Seen in': 'October build', 'Duplicate ids': 112, 'Caused by replacements': true } } },
  { kind: 'template', ref: 'before-after@1', title: 'Before / after slider', shows: 'Two versions of the same text, revealed with a slider.', allowed: TEMPLATE_ALLOWED,
    example: { widget: 'before-after@1', id: 'ex-before-after', title: 'valid_from type', data: { before: { title: 'DATE', lines: ['valid_from  date', 'tables affected: 3 of 40'] }, after: { title: 'TIMESTAMP', lines: ['valid_from  timestamp_ntz', 'tables affected: 0 of 40'] } } } },
  { kind: 'template', ref: 'line-chart@1', title: 'Line chart', shows: 'A small line over x with a value per point.', allowed: TEMPLATE_ALLOWED,
    example: { widget: 'line-chart@1', id: 'ex-line-chart', title: 'Duplicate ids per build', data: { title: 'Duplicate ids per build', unit: 'ids', points: [[1, 0], [2, 0], [3, 34], [4, 58], [5, 81], [6, 112]] } } },
  { kind: 'template', ref: 'option-prototype@1', title: 'Option prototype', shows: 'Options with cost and risk the reader can click through.', allowed: TEMPLATE_ALLOWED,
    example: { widget: 'option-prototype@1', id: 'ex-options', title: 'Which record wins?', data: { pick: 'latest', options: [{ id: 'latest', title: 'Latest valid_from', cost: 'low', risk: 'low', notes: 'Keeps the newest record.' }, { id: 'crm', title: 'CRM record', cost: 'low', risk: 'medium', notes: 'Trusts the CRM.' }] } } },
  { kind: 'template', ref: 'image-compare@1', title: 'Image compare', shows: 'Two screenshots, with a slider or side by side.', allowed: TEMPLATE_ALLOWED + ' Images only as inline data: URIs (PNG, JPEG, GIF, WebP); links and SVG are refused before the frame is drawn.',
    example: { widget: 'image-compare@1', id: 'ex-image-compare', title: 'Billing card, before and after', caption: 'Rows now line up; the colours follow the chart palette.', data: { alt: 'Billing card screenshot', before: { src: SAMPLE_BEFORE_PNG, label: 'main' }, after: { src: SAMPLE_AFTER_PNG, label: 'branch' } } } },
  { kind: 'template', ref: 'flow@1', title: 'Flow', shows: 'Boxes and arrows from JSON, laid out left to right; no loops.', allowed: TEMPLATE_ALLOWED,
    example: { widget: 'flow@1', id: 'ex-flow', title: 'Where duplicates come from', caption: 'A replacement creates a second meter row; dedup picks one.', data: { nodes: [{ id: 'crm', label: 'CRM export', status: 'done' }, { id: 'billing', label: 'Billing export', status: 'done' }, { id: 'stg', label: 'stg_meters', status: 'done' }, { id: 'dedup', label: 'Dedup rule', status: 'blocked' }, { id: 'dim', label: 'dim_meter', status: 'next' }], edges: [{ from: 'crm', to: 'stg' }, { from: 'billing', to: 'stg' }, { from: 'stg', to: 'dedup', label: '112 twice' }, { from: 'dedup', to: 'dim' }] } } },
  { kind: 'template', ref: 'table-explorer@1', title: 'Table explorer', shows: 'A table the reader can sort and filter.', allowed: TEMPLATE_ALLOWED,
    example: { widget: 'table-explorer@1', id: 'ex-table-explorer', title: 'Duplicated ids', data: { columns: ['meter_id', 'rows', 'latest valid_from', 'source'], rows: [['M-88213', 2, '2026-09-14', 'crm'], ['M-88540', 2, '2026-09-14', 'crm'], ['M-90102', 2, '2026-09-21', 'billing'], ['M-90377', 3, '2026-09-28', 'crm'], ['M-91004', 2, '2026-10-02', 'billing']] } } },
]

/** A template block with its pin: the template's current digest goes right after `widget`. */
export function pinned(block: Record<string, unknown>): Record<string, unknown> {
  if (typeof block.widget !== 'string') return block
  const t = findTemplate(block.widget)
  const { widget, ...rest } = block
  return { widget, sha256: t ? templateDigest(t) : '0'.repeat(64), ...rest }
}

/** The block text a person copies into a ticket section: a fenced `orch` block, pretty-printed. */
export function fenced(block: Record<string, unknown>): string {
  return '```orch\n' + JSON.stringify(pinned(block), null, 2) + '\n```'
}

/** The JSON inside the fence (what the parser reads). */
export function blockText(block: Record<string, unknown>): string {
  return JSON.stringify(pinned(block), null, 2)
}
