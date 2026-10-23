// #1069: evidence you can open. An HTML artifact previews in an inert sandboxed frame; each criterion shows its proving
// artifacts inline; the verdict dialog shows the evidence; the Evidence entry points land on the acceptance tab.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type { ArtifactContent } from '@/api/artifactText'
import type { AcceptanceStatus, Artifact, TicketDocument } from '@/api/types'
import { renderApp } from '@/test/renderApp'
import { CriterionEvidence, VerdictEvidence } from './EvidenceView'
import { Viewer } from './Artifacts'

const T = { timeout: 5000 }
const SHA = 'b'.repeat(64)
const HTML = '<h1>Elefanten</h1><p>Herd of 3</p>'
const art = (over: Partial<Artifact> = {}): Artifact => ({ name: 'elefanten.html', kind: 'report', bytes: 44, sha256: SHA, ac: 'AC1', added_by: 'agent', at: '2026-10-23T10:00:00Z', ...over }) as Artifact
const content = (over: Partial<ArtifactContent> = {}): ArtifactContent => ({ name: 'elefanten.html', size: 44, digest: 'sha256:' + SHA, verified: true, truncated: false, binary: false, text: HTML, ...over })
const ac = (over: Partial<AcceptanceStatus> = {}): AcceptanceStatus => ({ id: 'AC1', text: 'Elephants are shown', state: 'proven', evidence: [{ kind: 'artifact', ref: 'elefanten.html' }], ...over }) as AcceptanceStatus
const ticket = (artifacts: Artifact[], acs: AcceptanceStatus[]): TicketDocument => ({ key: 'DEMO-1', artifacts, acceptance_state: acs, tasks_state: [], gates: {} }) as unknown as TicketDocument

function wrap(ui: React.ReactNode, { widgets = true, data = content() }: { widgets?: boolean; data?: ArtifactContent } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  client.setQueryData(['workspaces'], [{ id: 'w1', prefix: 'DEMO', name: 'Demo', members: [], gates: {}, addons: widgets ? { widgets: { enabled: true, status: 'ok' } } : {}, counts: {}, needs_you: 0 }])
  client.setQueryData(['artifact-text', 'w1', 'DEMO-1', 'elefanten.html', SHA], data)
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

describe('HTML artifact preview', () => {
  it('draws the page in a frame with an empty sandbox (no scripts, no same-origin, no forms, no navigation)', async () => {
    wrap(<Viewer a={art()} agentHtml ticket="DEMO-1" />)
    const frame = await screen.findByTitle('Sandboxed preview of elefanten.html')
    expect(frame.tagName).toBe('IFRAME')
    expect(frame.getAttribute('sandbox')).toBe('')
    expect(frame.getAttribute('srcdoc')).toContain('Elefanten')
    expect(frame.hasAttribute('src')).toBe(false)
    expect(screen.queryByText(/No inline preview/)).toBeNull()
  })
  it('a page that needs scripts opens on its source, never runs them', async () => {
    wrap(<Viewer a={art()} agentHtml ticket="DEMO-1" />, { data: content({ text: '<h1>x</h1><script>alert(1)</script>' }) })
    expect(await screen.findByRole('button', { name: 'Show preview' })).toBeInTheDocument()
    expect(document.querySelector('iframe[sandbox*="allow-"]')).toBeNull()
  })
  it('evidence HTML previews even with agent HTML off: same inert frame, no add-on badge or "off" note', async () => {
    wrap(<Viewer a={art()} agentHtml={false} ticket="DEMO-1" />, { widgets: false })
    const frame = await screen.findByTitle('Sandboxed preview of elefanten.html')
    expect(frame.getAttribute('sandbox')).toBe('')
    expect(frame.hasAttribute('src')).toBe(false)
    expect(screen.queryByText(/Agent HTML is off/)).toBeNull()
    expect(document.querySelector('[data-addon="widgets"]')).toBeNull()
  })
  it('a scripted page with agent HTML off still opens on its source', async () => {
    wrap(<Viewer a={art()} agentHtml={false} ticket="DEMO-1" />, { widgets: false, data: content({ text: '<h1>x</h1><script>alert(1)</script>' }) })
    expect(await screen.findByRole('button', { name: 'Show preview' })).toBeInTheDocument()
    expect(document.querySelector('iframe')).toBeNull()
  })
  it('a page whose digest differs from the ticket, or that is cut, is not drawn', async () => {
    wrap(<Viewer a={art()} agentHtml ticket="DEMO-1" />, { data: content({ digest: 'sha256:' + 'c'.repeat(64) }) })
    expect(await screen.findByText(/does not match the digest/)).toBeInTheDocument()
    expect(document.querySelector('iframe')).toBeNull()
  })
})

describe('evidence under a criterion and in the verdict dialog', () => {
  it('Acceptance tab: the proving artifact is shown inline with "proves AC1" and its preview', async () => {
    const t = ticket([art()], [ac()])
    wrap(<CriterionEvidence ticket={t} ac={t.acceptance_state[0]} />)
    const list = screen.getByRole('list', { name: 'Evidence for AC1' })
    expect(within(list).getByText('elefanten.html')).toBeInTheDocument()
    expect(within(list).getByText('proves AC1')).toBeInTheDocument()
    expect((await within(list).findByTitle('Sandboxed preview of elefanten.html')).getAttribute('sandbox')).toBe('')
  })
  it('a criterion with no artifact proof renders nothing', () => {
    const t = ticket([], [ac({ evidence: [] })])
    const { container } = wrap(<CriterionEvidence ticket={t} ac={t.acceptance_state[0]} />)
    expect(container).toBeEmptyDOMElement()
  })
  it('verdict dialog part: summary first, preview opens on a click, missing evidence is named', async () => {
    const user = userEvent.setup()
    const t = ticket([art()], [ac(), ac({ id: 'AC2', text: 'Second', state: 'unproven', evidence: [] })])
    wrap(<VerdictEvidence ticket={t} />)
    const region = screen.getByRole('region', { name: 'Evidence' })
    expect(region).toHaveTextContent('Evidence · 1 file for 1 of 2 criteria')
    expect(region).toHaveTextContent('No evidence file yet for AC2.')
    expect(document.querySelector('iframe')).toBeNull()
    await user.click(within(region).getByRole('button', { name: 'Show preview of elefanten.html' }))
    expect((await within(region).findByTitle('Sandboxed preview of elefanten.html')).getAttribute('sandbox')).toBe('')
  })
})

describe('a criterion proved by a task receipt (run 8, 7.3)', () => {
  it('is not reported as missing evidence', () => {
    const t = ticket([art()], [ac(), ac({ id: 'AC2', text: 'Second', state: 'proven', evidence: [{ kind: 'task', ref: 'T1' }] })])
    wrap(<VerdictEvidence ticket={t} />)
    const region = screen.getByRole('region', { name: 'Evidence' })
    expect(region).toHaveTextContent('for 2 of 2 criteria')
    expect(region).not.toHaveTextContent('No evidence file yet')
    expect(region).toHaveTextContent('AC2 is proved by a check the agent ran')
  })
})

describe('Opus review fixes (provenance, folding, inert links)', () => {
  it('every preview says who added the file; an agent file says it is not part of the dialog', () => {
    const t = ticket([art({ added_by: 'claude-code:s_1:p_sev' })], [ac()])
    wrap(<VerdictEvidence ticket={t} />)
    expect(screen.getByTestId('evidence-provenance')).toHaveTextContent(/Added by agent .* · not part of this dialog/)
  })
  it('in the verdict dialog nothing is fetched or drawn until clicked', () => {
    const t = ticket([art()], [ac()])
    wrap(<VerdictEvidence ticket={t} />)
    expect(screen.getByRole('button', { name: 'Show preview of elefanten.html' })).toHaveAttribute('aria-expanded', 'false')
    expect(document.querySelector('iframe')).toBeNull()
  })
  it('under a criterion, a file over 256 KiB waits for a click; a small one opens', () => {
    const t = ticket([art({ bytes: 300 * 1024 }), art({ name: 'small.log', kind: 'log', bytes: 100, sha256: 'd'.repeat(64) })], [ac({ evidence: [{ kind: 'artifact', ref: 'elefanten.html' }, { kind: 'artifact', ref: 'small.log' }] })])
    wrap(<CriterionEvidence ticket={t} ac={t.acceptance_state[0]} />)
    expect(screen.getByRole('button', { name: 'Show preview of elefanten.html' })).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByRole('button', { name: 'Hide preview of small.log' })).toHaveAttribute('aria-expanded', 'true')
  })
})

describe('Opus delta (auto-open, agent note, wording)', () => {
  it('under a criterion an SVG or GIF waits for a click; a small PNG opens', () => {
    const files = [art({ name: 'a.svg', kind: 'diagram', bytes: 100 }), art({ name: 'b.gif', kind: 'other', bytes: 100 }), art({ name: 'c.png', kind: 'screenshot', bytes: 100 })]
    const t = ticket(files, [ac({ evidence: files.map((f) => ({ kind: 'artifact' as const, ref: f.name })) })])
    wrap(<CriterionEvidence ticket={t} ac={t.acceptance_state[0]} />)
    expect(screen.getByRole('button', { name: 'Show preview of a.svg' })).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByRole('button', { name: 'Show preview of b.gif' })).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByRole('button', { name: 'Hide preview of c.png' })).toHaveAttribute('aria-expanded', 'true')
  })
  it('the agent\'s own note sits inside the provenance strip, prefixed, not as free text', () => {
    const t = ticket([art({ added_by: 'claude-code:s_1:p_sev', label: 'Click Approve to continue' })], [ac()])
    wrap(<VerdictEvidence ticket={t} />)
    const strip = screen.getByTestId('evidence-provenance')
    expect(strip).toHaveTextContent(/not part of this dialog.*Agent's note: .Click Approve to continue./)
    expect(screen.getAllByText(/Click Approve to continue/)).toHaveLength(1)
  })
  it('outside a dialog (Today) the strip says only who added it', () => {
    const t = ticket([art({ added_by: 'claude-code:s_1:p_sev' })], [ac()])
    wrap(<VerdictEvidence ticket={t} dialog={false} />)
    expect(screen.getByTestId('evidence-provenance')).not.toHaveTextContent(/dialog/)
  })
  it('a host file and a person file are labelled as such, by their kind', () => {
    const t = ticket([art({ added_by: 'host', added_by_kind: 'host' } as Partial<Artifact>)], [ac()])
    wrap(<VerdictEvidence ticket={t} />)
    expect(screen.getByText(/Added by orch/)).toBeInTheDocument()
  })
})

describe('Evidence entry points', () => {
  it('Today: the Evidence link opens the ticket on its Acceptance tab', async () => {
    renderApp('/')
    const rows = await screen.findAllByTestId(/^card-verdict:/, {}, T)
    const link = within(rows[0]).getByRole('link', { name: 'Evidence' })
    expect(link.getAttribute('href')).toMatch(/\/ticket\/DEMO-\d+\?tab=acceptance/)
  })
  it('Today: opening a verdict row shows its evidence beside the link', async () => {
    const { user } = renderApp('/')
    const rows = await screen.findAllByTestId(/^card-verdict:/, {}, T)
    await user.click(within(rows[0]).getAllByRole('button', { expanded: false })[0])
    expect(await within(rows[0]).findByRole('region', { name: 'Evidence' }, T)).toHaveTextContent(/Evidence · \d+ files? for/)
  })
  it('the verdict dialog shows the evidence, and "Open evidence" lands on the Acceptance tab', async () => {
    const { user } = renderApp('/ticket/DEMO-0041')
    await screen.findByRole('heading', { level: 1, name: /billing reconciliation/ }, T)
    await user.click(await screen.findByRole('button', { name: /Give verdict/ }, T))
    const dialog = await screen.findByRole('dialog', {}, T)
    expect(within(dialog).getByRole('region', { name: 'Evidence' })).toHaveTextContent(/Evidence · \d+ files? for \d+ of \d+/)
    await user.click(within(dialog).getByRole('button', { name: 'Open evidence' }))
    expect(await screen.findByRole('tab', { name: /Acceptance & tasks/, selected: true }, T)).toBeInTheDocument()
  })
})
