import { TEMPLATES, templateDigest } from '@/api/widgetTemplates'
import { registerAddon } from './registry'

// widgets: rich ticket sections. The blocks themselves live in ticket text (format orch.widgets.v1) and are drawn by
// core on the ticket page: the four core types without a script, templates and one-off pages in the sandboxed frame.
// This addon is what turns the frame on (enabled and granted), and its page lists the templates with their pins. No state.

const CORE_ROWS = [
  { type: 'bars', shows: 'Horizontal labelled bars (label: number)' },
  { type: 'table', shows: 'A table, scrolling inside its own box when long' },
  { type: 'checks', shows: 'A verdict per acceptance criterion (the agent\'s check)' },
  { type: 'kv', shows: 'Facts as label and value' },
]

registerAddon({
  name: 'widgets',
  seed: () => ({}),
  view: () => ({
    templates: TEMPLATES.map((t) => ({ name: t.name, version: t.version, title: t.title, moment: t.moment, digest: templateDigest(t) })),
    coreTypes: CORE_ROWS.map((c) => ({ type: c.type })),
    templateRows: TEMPLATES.map((t) => ({ ref: `${t.name}@${t.version}`, title: t.title, moment: t.moment, pin: templateDigest(t).slice(0, 12) })),
    coreRows: CORE_ROWS,
  }),
  actions: {},
})
