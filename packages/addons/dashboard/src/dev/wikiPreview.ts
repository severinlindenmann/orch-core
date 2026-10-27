// Fixture of the Wiki layout preview (dev only): the node is the nav view that ships in packages/addons/wiki/views/wiki.json
// (a test compares them), and a few pages to read.
export const WIKI_PREVIEW_NODE = {
  type: 'stack',
  children: [
    { type: 'search', id: 'find', label: 'Search the wiki', placeholder: 'Search the wiki', read: 'wiki.search', target: 'pages' },
    {
      type: 'tree',
      id: 'pages',
      label: 'Wiki pages',
      read: 'wiki.tree',
      empty: 'The wiki folder has no pages yet. Add .md files to it.',
      detail: { read: 'wiki.page', title: 'title', text: 'markdown', empty: 'This page is too large to show here.' },
    },
  ],
}

export const WIKI_PREVIEW_PAGES: { id: string; title: string; markdown: string; badge?: string }[] = [
  {
    id: 'on-call-runbook',
    title: 'On-call runbook',
    badge: '2 linked tickets',
    markdown:
      '# On-call runbook\n\nWhat to do when the pager goes off. Read this once, keep it open during a shift.\n\n## First five minutes\n\n1. Acknowledge the alert so nobody else starts the same work.\n2. Open the status page and check whether it is a known incident.\n3. Post a one-line note in the team channel: what you see, what you try first.\n\n## Escalation\n\n| Severity | Who | Within |\n| --- | --- | --- |\n| 1 | Team lead, then the owner | 5 minutes |\n| 2 | Team lead | 30 minutes |\n| 3 | Next working day | n/a |\n\n> If you are unsure, escalate. Nobody has ever been blamed for waking someone up too early.\n\n## Useful commands\n\n```sh\norch ticket list --status open\norch ticket show DEMO-0042\n```\n\nSee also the [incident template](https://example.com/incident-template).\n',
  },
  { id: 'reconciliation-tolerance', title: 'Reconciliation tolerance', badge: '1 linked ticket', markdown: '# Reconciliation tolerance\n\nPayments within **0.05 CHF** of the invoice amount count as matched.\n\n- Rounding differences below the tolerance are booked automatically.\n- Anything above goes to the review queue.\n' },
  { id: 'release-checklist', title: 'Release checklist', markdown: '# Release checklist\n\n- [ ] Tests are green on develop\n- [ ] Changelog updated\n- [ ] Owner has approved the release ticket\n' },
  { id: 'glossary', title: 'Glossary', markdown: '# Glossary\n\n**Ticket**: one piece of work with a clear end.\n\n**Gate**: a check a person must approve before work moves on.\n' },
  { id: 'imported-from-old-wiki', title: 'Imported from old wiki', markdown: '# Imported from old wiki\n\nKept for reference. Needle for the search test: the reconciliation job runs nightly.\n' },
]
