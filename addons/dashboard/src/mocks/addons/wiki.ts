import { registerAddon } from './registry'

// wiki: markdown pages in the workspace, linked from tickets. Pages, the open page (`current`) and the search
// query live in the addon state, so they are per workspace (not per viewer): opening a page changes it for everyone.
// The ticket panel reads `addon.byTicket.$ticket`, generated in view() from each page's `tickets`.
// Page text is untrusted markdown: the UI renders it only through SafeMarkdown.

interface Page {
  slug: string
  title: string
  markdown: string
  updated: string
  by: string
  tickets: string[]
}

const DAY_MS = 86_400_000
const EPOCH = Date.UTC(2026, 9, 9, 11, 30)
const daysAgo = (n: number, hour = 9) => new Date(Date.UTC(2026, 9, 9 - n, hour, 0)).toISOString().replace(/\.\d{3}Z$/, 'Z')

const PAGES: Page[] = [
  {
    slug: 'tariff-data-conventions',
    title: 'Tariff data conventions',
    updated: daysAgo(2),
    by: 'Mara',
    tickets: ['DEMO-0041', 'DEMO-0043'],
    markdown: `# Tariff data conventions

How we name, version and review tariff data in the **Acme energy data** workspace.

## Rules

- One seed file per tariff table, named \`tariff_<region>_<year>.csv\`.
- Every table has a \`valid_from\` date; never edit a past row, add a new one.
- Changes go through a pull request with a reconciliation run attached.

## Checks

| Check | Where | Tolerance |
| --- | --- | --- |
| Row count | dbt test | exact |
| Billed kWh vs finance | reconciliation | 0.1 % |
| Price per kWh | dbt test | between 0.05 and 0.60 CHF |

## Loader query

\`\`\`sql
select tariff_id, valid_from, price_per_kwh
from {{ ref('tariff_ch_2026') }}
where valid_from <= current_date;
\`\`\`

See the [dbt seeds guide](https://docs.getdbt.com/docs/build/seeds) for the loader.

> Seeds are for small, slow-changing reference data only.
`,
  },
  {
    slug: 'dbt-seeds',
    title: 'dbt seeds: when to use them',
    updated: daysAgo(9),
    by: 'Severin',
    tickets: ['DEMO-0041'],
    markdown: `# dbt seeds: when to use them

Use a seed when the data is small (under 5,000 rows), changes a few times a year and has a human owner.

- Tariff tables: yes.
- Meter readings: no, those come from the ingestion job.
- Holiday calendars: yes, one file per country.

Run \`dbt seed --select tag:tariff\` after every change and commit the resulting manifest.
`,
  },
  {
    slug: 'reconciliation-tolerance',
    title: 'Reconciliation tolerance',
    updated: daysAgo(1, 14),
    by: 'Mara',
    tickets: ['DEMO-0043'],
    markdown: `# Reconciliation tolerance

The monthly reconciliation compares billed kWh against the finance total.

1. Default tolerance is **0.1 %** of the finance total.
2. A deviation above tolerance fails the dbt test and names the three biggest deltas.
3. Finance may raise the tolerance for one month in writing; record it in the pull request.

Anything above 1 % is treated as a data incident, not a rounding problem.
`,
  },
  {
    slug: 'on-call-runbook',
    title: 'On-call runbook',
    updated: daysAgo(5),
    by: 'Severin',
    tickets: [],
    markdown: `# On-call runbook

## When the nightly load is late

1. Check the ingestion dashboard for the last successful partition.
2. Re-run the failed task once. If it fails again, page the secondary.
3. Post in the data channel with the partition date and the error.

## Contacts

| Role | Who | Hours |
| --- | --- | --- |
| Primary | Mara | 08:00 to 18:00 |
| Secondary | Severin | 24 h, escalation only |

Never delete a partition to make a run pass.
`,
  },
  {
    slug: 'glossary',
    title: 'Glossary',
    updated: daysAgo(14),
    by: 'Mara',
    tickets: [],
    markdown: `# Glossary

- **kWh**: kilowatt hour, the billing unit for electricity.
- **Tariff**: a price per kWh valid from a given date for a region.
- **Settlement**: the monthly match of metered against billed energy.
- **Partition**: one day of ingested meter readings.
`,
  },
  {
    slug: 'dbt-model-naming',
    title: 'dbt model naming',
    updated: daysAgo(21),
    by: 'Severin',
    tickets: ['DEMO-0042'],
    markdown: `# dbt model naming

- \`stg_<source>__<table>\` for staging models, one per source table.
- \`int_<topic>\` for intermediate steps that are never exposed.
- \`fct_<event>\` and \`dim_<thing>\` for the marts.

Keep timestamps in UTC and name the column \`<event>_at\`.
`,
  },
  {
    slug: 'imported-from-old-wiki',
    title: 'Imported from old wiki',
    updated: daysAgo(30),
    by: 'Tom',
    tickets: [],
    // Deliberately hostile: this page exists to prove that SafeMarkdown renders addon markdown inert.
    markdown: `# Imported from old wiki

Pasted as is from the old system. Some of it is not safe.

<script>window.__pwned = 1</script>

<img src="https://example.com/x.png" onerror="window.__pwned = 2">

[x](javascript:alert(1))

[y](data:text/html,<script>alert(1)</script>)

A normal link still works: [dbt docs](https://docs.getdbt.com).
`,
  },
]

const ago = (iso: string) => {
  const d = Math.max(0, Math.floor((EPOCH - Date.parse(iso)) / DAY_MS))
  return d === 0 ? 'updated today' : `updated ${d}d ago`
}

const pagesOf = (state: Record<string, unknown>) => state.pages as Page[]
const find = (state: Record<string, unknown>, key: unknown) =>
  typeof key === 'string' ? pagesOf(state).find((p) => p.slug === key || p.title === key) : undefined

const item = (p: Page, open: boolean) => ({
  title: p.title,
  subtitle: `by ${p.by} · wiki/${p.slug}.md`,
  badge: ago(p.updated),
  ...(open ? { actions: [{ action: 'open', label: 'Open', args: { slug: p.slug } }] } : {}),
})

registerAddon({
  name: 'wiki',
  seed: () => ({ settings: {}, pages: structuredClone(PAGES), current: PAGES[0].slug, query: '' }),
  view(state) {
    const pages = pagesOf(state)
    const q = String(state.query ?? '').trim().toLowerCase()
    const shown = q ? pages.filter((p) => `${p.title}\n${p.markdown}`.toLowerCase().includes(q)) : pages
    const cur = find(state, state.current) ?? pages[0]
    const byTicket: Record<string, ReturnType<typeof item>[]> = {}
    for (const p of pages) for (const t of p.tickets) (byTicket[t] ??= []).push(item(p, false))
    return {
      items: shown.map((p) => item(p, true)),
      current: cur ? { slug: cur.slug, title: cur.title, markdown: cur.markdown, updated: cur.updated, by: cur.by } : { slug: '', title: '', markdown: 'No page selected.', updated: '', by: '' },
      editForm: cur ? { title: cur.title, markdown: cur.markdown } : null,
      searchForm: { query: String(state.query ?? '') },
      titles: pages.map((p) => p.title),
      slugs: pages.map((p) => p.slug),
      byTicket,
    }
  },
  actions: {
    open({ state, body }) {
      const p = find(state, body.slug)
      if (!p) return { ok: true, message: 'Pick a page to open.' }
      state.current = p.slug
      return { ok: true, message: `Opened ${p.title}.`, changed: true }
    },
    save({ state, body, store, ws, viewer }) {
      const p = find(state, state.current)
      const data = (body.formData ?? {}) as { title?: unknown; markdown?: unknown }
      if (!p || typeof data.title !== 'string' || typeof data.markdown !== 'string' || !data.title.trim()) return { ok: true, message: 'A page needs a title.' }
      p.title = data.title.trim()
      p.markdown = data.markdown
      p.updated = store.now()
      p.by = store.workspaces.find((w) => w.id === ws)?.members.find((m) => m.person === viewer)?.name ?? viewer
      return { ok: true, message: `Saved ${p.title}.`, changed: true }
    },
    search({ state, body }) {
      const data = (body.formData ?? {}) as { query?: unknown }
      state.query = typeof data.query === 'string' ? data.query : ''
      return { ok: true, message: state.query ? `Filtered by "${state.query}".` : 'Showing all pages.', changed: true }
    },
    link({ state, body, ticket, store }) {
      const p = find(state, (body.formData as { page?: unknown } | undefined)?.page)
      if (!ticket || !store.hasTicket(ticket)) return { ok: true, message: 'Pick a ticket first.' }
      if (!p) return { ok: true, message: 'Pick a page to link.' }
      if (!p.tickets.includes(ticket)) p.tickets = [...p.tickets, ticket]
      return { ok: true, message: `Linked ${p.title} to ${ticket}.`, changed: true }
    },
    save_settings: {
      minRole: 'owner',
      run: ({ state, body }) => {
        state.settings = body.formData ?? {}
        return { ok: true, message: 'Settings saved.', changed: true }
      },
    },
  },
})
