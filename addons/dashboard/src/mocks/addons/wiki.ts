import { briefs, dayIso } from '../busy/helpers'
import type { Rng } from '../busy/rng'
import type { MockStore } from '../store'
import { canSeeTicket, conflict, invalid, notFound, registerAddon } from './registry'

// wiki: markdown pages in the workspace, linked from tickets. Pages are shared per workspace; which page a person has
// open and their search query are per viewer (`state.nav[viewer] = { current, query }`), so navigating never affects
// anyone else. `open` and `search` are minRole 'viewer' in the manifest (viewers can read and navigate; only editing
// needs member). Pages are always addressed by slug; titles are labels and must be unique.
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
const navOf = (state: Record<string, unknown>) => (state.nav ??= {}) as Record<string, { current?: string; query?: string }>
const bySlug = (state: Record<string, unknown>, slug: unknown) => (typeof slug === 'string' ? pagesOf(state).find((p) => p.slug === slug) : undefined)

const item = (p: Page, open: boolean) => ({
  title: p.title,
  subtitle: `by ${p.by} · wiki/${p.slug}.md`,
  badge: ago(p.updated),
  ...(open ? { actions: [{ action: 'open', label: 'Open', args: { slug: p.slug } }] } : {}),
})

const TOPICS = ['Tariff data', 'Meter readings', 'Billing runs', 'Reconciliation', 'dbt seeds', 'Ingestion', 'Invoices', 'Credit notes', 'Daylight saving', 'Warehouse access', 'Finance export', 'Smart meters', 'Heat pumps', 'Solar feed-in', 'Outage events', 'Customer segments']
const FORMS = ['conventions', 'runbook', 'how-to', 'FAQ', 'decision record', 'checklist']

/** Busy day: pages up to 30 in all (a third of that outside DEMO), each linked to up to three tickets. */
function busyPages(ws: string, store: MockStore, rng: Rng): Page[] {
  const demo = store.workspaces.find((w) => w.id === ws)?.prefix === 'DEMO'
  const have = new Set(PAGES.map((p) => p.title))
  const tickets = briefs(store, ws)
  const pages: Page[] = []
  const want = demo ? 30 - PAGES.length : 8
  for (const topic of rng.shuffle(TOPICS)) {
    for (const form of rng.shuffle(FORMS)) {
      if (pages.length >= want) return pages
      const title = `${topic} ${form}`
      if (have.has(title) || rng.chance(0.55)) continue
      have.add(title)
      const slug = title.toLowerCase().replace(/[^a-z0-9]+/g, '-')
      pages.push({
        slug,
        title,
        updated: dayIso(rng.int(0, 28), rng.int(7, 17)),
        by: rng.pick(['Mara', 'Severin']),
        tickets: rng.sample(tickets, rng.int(0, 3)).map((t) => t.key),
        markdown: `# ${title}\n\nHow the Acme energy data team handles ${topic.toLowerCase()}: ${form}.\n\n## Rules\n\n${Array.from({ length: rng.int(3, 7) }, (_, i) => `${i + 1}. ${rng.pick(['Name the owner in the pull request.', 'Check the September data first.', 'Never edit a past row, add a new one.', 'Run the reconciliation before the close.', 'Keep timestamps in UTC.', 'Write down the tolerance you used.'])}`).join('\n')}\n\n## Checks\n\n| Check | Where | Tolerance |\n| --- | --- | --- |\n| Row count | dbt test | exact |\n| Billed kWh | reconciliation | 0.1 % |\n\n\`\`\`sql\nselect count(*) from {{ ref('fct_billing') }};\n\`\`\`\n`,
      })
    }
  }
  // If the random skips left us short, fill with numbered pages.
  for (let n = 1; pages.length < want; n++) pages.push({ slug: `notes-${n}`, title: `Team notes ${n}`, updated: dayIso(n), by: 'Mara', tickets: [], markdown: `# Team notes ${n}\n\nShort notes.\n` })
  return pages
}

registerAddon({
  name: 'wiki',
  seed: () => ({ settings: {}, pages: structuredClone(PAGES), nav: {} }),
  seedBusy: (ws, store, rng) => ({ settings: {}, pages: [...structuredClone(PAGES), ...busyPages(ws, store, rng)], nav: {} }),
  view(state, c) {
    const { viewer } = c
    // Pages are shared; which tickets they link to is shown only for tickets this viewer can see.
    const pages = pagesOf(state).map((p) => ({ ...p, tickets: p.tickets.filter((t) => canSeeTicket(c, t)) }))
    const nav = ((state.nav ?? {}) as ReturnType<typeof navOf>)[viewer] ?? {} // read-only: never create state.nav here
    const query = nav.query ?? ''
    const q = query.trim().toLowerCase()
    const shown = q ? pages.filter((p) => `${p.title}\n${p.markdown}`.toLowerCase().includes(q)) : pages
    const cur = bySlug(state, nav.current) ?? pages[0]
    const byTicket: Record<string, ReturnType<typeof item>[]> = {}
    for (const p of pages) for (const t of p.tickets) (byTicket[t] ??= []).push(item(p, false))
    return {
      pages, // overrides the raw list
      items: shown.map((p) => item(p, true)),
      current: cur
        ? { slug: cur.slug, title: cur.title, markdown: cur.markdown, updated: cur.updated, by: cur.by, meta: `by ${cur.by} · ${ago(cur.updated)}` }
        : { slug: '', title: '', markdown: 'No page selected.', updated: '', by: '', meta: '' },
      editForm: cur ? { slug: cur.slug, title: cur.title, markdown: cur.markdown } : null,
      searchForm: { query },
      pageOptions: pages.map((p) => ({ const: p.slug, title: p.title })),
      byTicket,
    }
  },
  actions: {
    open({ state, body, viewer }) {
      const p = bySlug(state, body.slug)
      if (!p) return notFound('That page no longer exists.')
      navOf(state)[viewer] = { ...navOf(state)[viewer], current: p.slug }
      return { ok: true, message: `Opened ${p.title}.`, changed: true }
    },
    search({ state, body, viewer }) {
      const data = (body.formData ?? {}) as { query?: unknown }
      const query = typeof data.query === 'string' ? data.query : ''
      navOf(state)[viewer] = { ...navOf(state)[viewer], query }
      return { ok: true, message: query ? `Filtered by "${query}".` : 'Showing all pages.', changed: true }
    },
    save({ state, body, store, ws, viewer }) {
      const data = (body.formData ?? {}) as { slug?: unknown; title?: unknown; markdown?: unknown }
      const p = bySlug(state, data.slug) // the page the form was opened on, never "whatever is current now"
      if (!p) return notFound('That page no longer exists.')
      const title = typeof data.title === 'string' ? data.title.trim() : ''
      if (!title || typeof data.markdown !== 'string') return invalid('A page needs a title.')
      if (pagesOf(state).some((o) => o !== p && o.title.toLowerCase() === title.toLowerCase())) return conflict('wiki.title_taken', `A page called "${title}" already exists.`, 'Pick another title.')
      p.title = title
      p.markdown = data.markdown
      p.updated = store.now()
      p.by = store.workspaces.find((w) => w.id === ws)?.members.find((m) => m.person === viewer)?.name ?? viewer
      return { ok: true, message: `Saved ${p.title}.`, changed: true }
    },
    link(ctx) {
      const { state, body, ticket } = ctx
      const p = bySlug(state, (body.formData as { page?: unknown } | undefined)?.page)
      if (!ticket || !canSeeTicket(ctx, ticket)) return invalid('Pick a ticket first.')
      if (!p) return invalid('Pick a page to link.')
      if (!p.tickets.includes(ticket)) p.tickets = [...p.tickets, ticket]
      return { ok: true, message: `Linked ${p.title} to ${ticket}.`, changed: true }
    },
    save_settings: ({ state, body }) => {
      state.settings = body.formData ?? {}
      return { ok: true, message: 'Settings saved.', changed: true }
    },
  },
})
