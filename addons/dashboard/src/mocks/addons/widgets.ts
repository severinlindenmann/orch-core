import { blockText, CATALOG, CORE_ALLOWED, PROPOSED_NOTE, TEMPLATE_ALLOWED, type CatalogEntry } from '@/api/widgetCatalog'
import { findTemplate, TEMPLATES, templateDigest } from '@/api/widgetTemplates'
import { registerAddon } from './registry'

// widgets: rich ticket sections. The blocks themselves live in ticket text (format orch.widgets.v1) and are drawn by
// core on the ticket page: core types without a script, templates and one-off pages in the sandboxed frame. This
// addon is what turns the frame on (enabled and granted), and its page is the gallery: every type with a rendered
// example (a core-drawn `widget` node), its copyable source and where it is allowed. No state.

/**
 * "Where it's allowed" is said once per section (its intro); an entry repeats it only where it differs: an extra rule
 * after the section's sentence ("Also: …"), or a rule of its own.
 */
export function allowedNote(c: CatalogEntry): string {
  const base = c.kind === 'core' ? CORE_ALLOWED : TEMPLATE_ALLOWED
  if (c.allowed === base) return ''
  if (c.allowed.startsWith(base)) return `\n\n**Also:** ${c.allowed.slice(base.length).trim()}`
  return `\n\n**Where it's allowed:** ${c.allowed}`
}

const entry = (c: CatalogEntry) => {
  const t = c.kind === 'template' ? findTemplate(c.ref) : undefined
  const pin = t ? `\n\nPin (sha256): \`${templateDigest(t).slice(0, 12)}…\` · moment: ${t.moment}` : ''
  return {
    type: 'stack',
    children: [
      { type: 'markdown', text: `### ${c.title} · \`${c.ref}\`\n\n${c.proposed ? `\`${PROPOSED_NOTE}\` ` : ''}${c.shows}${c.proposedForm ? ` \`${PROPOSED_NOTE}\`: ${c.proposedForm}.` : ''}${allowedNote(c)}${pin}` },
      { type: 'widget', block: blockText(c.example), source: true },
    ],
  }
}

const core = CATALOG.filter((c) => c.kind === 'core')
const templates = CATALOG.filter((c) => c.kind === 'template')

registerAddon({
  name: 'widgets',
  seed: () => ({}),
  view: () => ({
    templates: TEMPLATES.map((t) => ({ name: t.name, version: t.version, title: t.title, moment: t.moment, digest: templateDigest(t) })),
    coreTypes: core.map((c) => ({ type: c.ref })),
    gallery: [
      {
        type: 'widget-index',
        groups: [
          { label: 'Core', items: core.map((c) => ({ label: c.ref, widget: String(c.example.id) })) },
          { label: 'Templates', items: templates.map((c) => ({ label: c.ref, widget: String(c.example.id) })) },
        ],
      },
      { type: 'markdown', text: `## Core types (${core.length})\n\nDrawn by core: no script, colours from the dashboard palette, a text alternative behind "Show text". Names and shapes follow orch.widgets.v1; types marked Proposed are not in v1 yet.\n\n**Where it's allowed:** ${CORE_ALLOWED}` },
      ...core.map(entry),
      { type: 'markdown', text: `## Templates (${templates.length})\n\nAgent HTML, reused: it runs in a sandboxed frame with no network and no navigation, marked with the orange A. Core checks each block's data before the frame gets it.\n\n**Where it's allowed:** ${TEMPLATE_ALLOWED}` },
      ...templates.map(entry),
    ],
  }),
  actions: {},
})
