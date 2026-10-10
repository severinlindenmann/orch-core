// Mandates, PREVIEW ONLY (M1): Agents → Mandates, the shell banner, Today's "Decided for you", the demo toggle, and
// the guarantee that the preview never reaches a signing or decision path.
import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { PREVIEW_LINE } from '@/api/mandatesPreview'
import { NEVER, PROTECTED_PATHS } from '@/mocks/mandates-preview'
import type { MockStore } from '@/mocks/store'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 8000 }
const wsOf = (s: MockStore) => s.workspaces.find((w) => w.prefix === 'DEMO')!.id
/** The preview on with the seeded mandate md_3 in force (as the Demo data toggle does). */
const seeded = (s: MockStore) => {
  const r = s.mandatesPreview.request(wsOf(s), { op: 'enable', seed: true })
  if (!r.ok) throw new Error(r.message)
}
const banner = () => screen.queryByTestId('mandate-banner')

/** Every api method that signs or decides something real. The preview must never call one. */
const SIGNING = ['postAction', 'runAddonAction', 'issueGrant', 'revokeGrant', 'grantSkillCredentials', 'postAddonOp', 'postRelay', 'postSettings'] as const

afterEach(() => {
  vi.restoreAllMocks()
  sessionStorage.clear()
})

describe('Agents → Mandates (preview)', () => {
  it('opens from the address, carries the preview line, and the tab click keeps the address', async () => {
    const { user, address } = renderApp('/agents?tab=mandates')
    const tab = await screen.findByRole('tab', { name: /Mandates/ }, T)
    expect(tab).toHaveAttribute('aria-selected', 'true')
    expect(within(tab).getByText('Preview')).toBeInTheDocument()
    expect(await screen.findByText(PREVIEW_LINE, {}, T)).toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: 'Sessions' }))
    await waitFor(() => expect(address()).not.toContain('tab='))
    expect(await screen.findByRole('region', { name: /Working/ }, T)).toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: /Mandates/ }))
    await waitFor(() => expect(address()).toContain('tab=mandates'))
  })

  it('the preflight blocks issuing until "Show the pilot anyway"', async () => {
    const { user } = renderApp('/agents?tab=mandates')
    await screen.findByTestId('mandates-tab', {}, T)
    for (const id of ['p1', 'p2', 'p3', 'p4']) expect(screen.getByTestId(`preflight-${id}`)).toHaveTextContent(/not available in this build/)
    expect(screen.getByTestId('preflight-p1')).toHaveTextContent(/P2 custody/)
    expect(screen.getByTestId('preflight-blocked')).toHaveTextContent(/Issuing is blocked/)
    const issue = screen.getByRole('button', { name: 'Issue a pilot mandate…' })
    expect(issue).toBeDisabled()
    expect(screen.getByTestId('issue-blocked')).toBeInTheDocument()
    const anyway = screen.getByRole('switch', { name: /Show the pilot anyway \(preview\)/ })
    expect(anyway).toHaveAttribute('aria-checked', 'false')
    await user.click(anyway)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Issue a pilot mandate…' })).toBeEnabled())
    // Turning the preview on this way issues nothing: no banner yet.
    expect(banner()).toBeNull()
  })

  it('the issue dialog shows the fixed scope, the never list and protected paths in full; signing it signs nothing', async () => {
    const { user } = renderApp('/agents?tab=mandates', { setup: (s) => void s.mandatesPreview.request(wsOf(s), { op: 'enable' }) })
    await user.click(await screen.findByRole('button', { name: 'Issue a pilot mandate…' }, T))
    const dialog = await screen.findByRole('dialog', { name: 'Issue a pilot mandate' }, T)
    expect(within(dialog).getByText(PREVIEW_LINE)).toBeInTheDocument()
    const covers = within(dialog).getByTestId('preview-covers')
    expect(covers).toHaveTextContent('Decides: requirements approvals, plan approvals, verdicts (fixed in the pilot)')
    expect(covers).toHaveTextContent(/Admits epic DEMO-0050 Monthly billing v2 and its children up to size m/)
    expect(covers).toHaveTextContent(/Duration: 7 days, until .* UTC · no renewal/)
    expect(covers).toHaveTextContent(/Orchestrator: /)
    // Decision kinds, size and duration are shown, not editable: only the orchestrator and the epic are inputs.
    expect(within(dialog).getAllByRole('combobox').map((c) => c.id)).toEqual(['mandate-orch', 'mandate-epic'])
    expect(within(dialog).queryByRole('textbox')).toBeNull()
    const never = within(dialog).getByTestId('mandate-never')
    expect(within(never).getAllByRole('listitem').map((li) => li.textContent)).toEqual(NEVER)
    expect(within(within(dialog).getByTestId('mandate-protected')).getAllByRole('listitem').map((li) => li.textContent)).toEqual(PROTECTED_PATHS)
    await user.click(within(dialog).getByRole('button', { name: 'Sign mandate (preview — nothing is signed)' }))
    expect(await screen.findByTestId('mandate-log', {}, T)).toBeInTheDocument()
    expect(await screen.findByTestId('mandate-banner', {}, T)).toHaveTextContent(/Mandate md_3 · for Severin · epic DEMO-0050 · 9 decisions · until Tue/)
  })

  it('the mandate in force: limits as meters, the decision log in core words, refused items with reasons, revisions', async () => {
    renderApp('/agents?tab=mandates', { setup: seeded })
    const log = await screen.findByTestId('mandate-log', {}, T)
    const rows = within(log).getAllByRole('listitem')
    expect(rows).toHaveLength(9)
    expect(log).toHaveTextContent(/Verdict: via mandate md_3, for Severin — no person reviewed this \(commit b7e1f02\)/)
    expect(log).toHaveTextContent(/checked by checker si_chk_4f2a \(passed\)/)
    const limits = screen.getByTestId('mandate-limits')
    expect(within(limits).getByRole('progressbar', { name: 'Decisions' })).toHaveAttribute('aria-valuenow', '9')
    expect(within(limits).getByRole('progressbar', { name: 'Days left' })).toHaveAttribute('aria-valuemax', '7')
    expect(within(limits).getByRole('progressbar', { name: 'Children' })).toBeInTheDocument()
    const refused = screen.getByTestId('mandate-refused')
    expect(refused).toHaveTextContent('Protected path')
    expect(refused).toHaveTextContent('Your veto')
    expect(refused).toHaveTextContent('Limit reached')
    expect(screen.getByText(/Revision 1/)).toBeInTheDocument()
  })

  it('Revoke and void lists exactly the decisions on work not landed, and the landed ones for review', async () => {
    const { user } = renderApp('/agents?tab=mandates', { setup: seeded })
    await user.click(await screen.findByRole('button', { name: 'Revoke and void…' }, T))
    const dialog = await screen.findByRole('alertdialog', {}, T)
    expect(dialog).toHaveTextContent('Revoke mandate md_3 and void its decisions?')
    const voids = within(within(dialog).getByTestId('revoke-voids')).getAllByRole('listitem').map((li) => li.textContent!.slice(0, 14))
    // Seeded: #1831–#1839, the verdicts #1833 and #1836 landed.
    expect(voids).toEqual(['#1831DEMO-0051', '#1832DEMO-0051', '#1834DEMO-0053', '#1835DEMO-0053', '#1837DEMO-0052', '#1838DEMO-0052', '#1839DEMO-0054'])
    const landed = within(within(dialog).getByTestId('revoke-landed')).getAllByRole('listitem').map((li) => li.textContent!.slice(0, 5))
    expect(landed).toEqual(['#1833', '#1836'])
    expect(within(dialog).getByTestId('preview-covers')).toHaveTextContent('Voids 7 decisions on work that has not landed')
    await user.click(within(dialog).getByRole('button', { name: 'Revoke and void (preview — nothing is signed)' }))
    expect(await screen.findByText(/Revoked .*: 7 decisions voided, 2 on landed work listed for your review/, {}, T)).toBeInTheDocument()
    await waitFor(() => expect(banner()).toBeNull())
  })
})

describe('shell banner (preview)', () => {
  it('appears with the preview on, is calm and one line, hides for the session, and goes when the preview is turned off', async () => {
    const { user } = renderApp('/board')
    await screen.findByRole('heading', { level: 1, name: /Board/ }, T)
    expect(banner()).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Preview: mandates' }))
    const b = await screen.findByTestId('mandate-banner', {}, T)
    expect(b).toHaveTextContent(/Mandate md_3 · for Severin · epic DEMO-0050 · 9 decisions · until Tue/)
    expect(within(b).getByText('Preview')).toBeInTheDocument()
    expect(within(b).getByRole('link', { name: 'Details' }).getAttribute('href')).toMatch(/\/agents\?tab=mandates$/)
    expect(b.className).not.toMatch(/warning|danger|addon|orange/)
    expect(b.className).toMatch(/h-9/)
    await user.click(within(b).getByRole('button', { name: /Hide the mandate banner/ }))
    expect(banner()).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Preview: mandates' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Preview: mandates' })).toHaveAttribute('aria-pressed', 'false'))
    expect(banner()).toBeNull()
  })

  it('Stop: "Stopping…" until the host acknowledges, then "Stopped at #1842"', async () => {
    const { user } = renderApp('/agents?tab=mandates', { setup: seeded })
    const b = await screen.findByTestId('mandate-banner', {}, T)
    await user.click(within(b).getByRole('button', { name: 'Stop' }))
    const dialog = await screen.findByRole('dialog', { name: 'Stop mandate md_3?' }, T)
    await user.click(within(dialog).getByRole('checkbox', { name: 'Also stop agents' }))
    expect(within(dialog).getByTestId('preview-covers')).toHaveTextContent(/Also stops the agents/)
    await user.click(within(dialog).getByRole('button', { name: 'Stop mandate (preview — nothing is signed)' }))
    await waitFor(() => expect(screen.getByTestId('mandate-banner')).toHaveTextContent('Stopping…'))
    await waitFor(() => expect(screen.getByTestId('mandate-banner')).toHaveTextContent('Stopped at #1842'), T)
    expect(await screen.findByText(/Stopped at #1842: nothing after it is signed\. Also stop agents was asked \(preview: no agent was stopped\)\./, {}, T)).toBeInTheDocument()
  })
})

describe('Today: Decided for you (preview)', () => {
  it('lists the decisions since the last look with Looks right, Veto and Revoke and void, and the refused items as needs-you rows', async () => {
    const { user } = renderApp('/', { setup: seeded })
    const digest = await screen.findByTestId('decided-for-you', {}, T)
    expect(digest).toHaveTextContent(/Decided for you · 5 since you last looked · via mandate md_3, epic DEMO-0050/)
    expect(digest).toHaveTextContent(PREVIEW_LINE)
    const rows = () => within(digest).queryAllByTestId(/^mandate-decision:/)
    expect(rows()).toHaveLength(5)
    expect(rows()[0]).toHaveTextContent(/DEMO-0054Requirements approved: via mandate md_3, for Severin — no person reviewed this/)
    await user.click(within(rows()[0]).getByRole('button', { name: 'Looks right' }))
    await waitFor(() => expect(rows()).toHaveLength(4))
    await user.click(within(rows()[0]).getByRole('button', { name: 'Veto' }))
    await waitFor(() => expect(rows()).toHaveLength(3))
    const refused = screen.getByTestId('mandate-refused-today')
    expect(within(refused).getAllByTestId(/^mandate-refused:/)).toHaveLength(3)
    expect(refused).toHaveTextContent(/Protected path:.*package-lock\.json/)
    await user.click(within(rows()[0]).getByRole('button', { name: 'Revoke and void' }))
    const dialog = await screen.findByRole('alertdialog', {}, T)
    expect(within(within(dialog).getByTestId('revoke-voids')).getAllByRole('listitem')).toHaveLength(7)
    await user.click(within(dialog).getByRole('button', { name: 'Revoke and void (preview — nothing is signed)' }))
    await waitFor(() => expect(screen.queryByTestId('decided-for-you')).toBeNull())
    expect(screen.queryByTestId('mandate-refused-today')).toBeNull()
  })
})

describe('preview off (the default)', () => {
  it('Today and the shell look as before: no banner, no digest, the Sessions tab first', async () => {
    renderApp('/')
    await screen.findByRole('region', { name: 'Needs you' }, T)
    expect(banner()).toBeNull()
    expect(screen.queryByTestId('decided-for-you')).toBeNull()
    expect(screen.queryByTestId('mandate-refused-today')).toBeNull()
    expect(screen.queryByText(PREVIEW_LINE)).toBeNull()
    expect(screen.getByRole('button', { name: 'Preview: mandates' })).toHaveAttribute('aria-pressed', 'false')
  })
  it('the demo Reset turns the preview off again', async () => {
    const { user } = renderApp('/', { setup: seeded })
    await screen.findByTestId('mandate-banner', {}, T)
    await user.click(screen.getByRole('button', { name: 'Reset demo' }))
    await user.click(await screen.findByRole('button', { name: 'Reset demo data' }, T))
    await waitFor(() => expect(banner()).toBeNull(), T)
    expect(screen.queryByTestId('decided-for-you')).toBeNull()
  })
  it('a viewer who is not an owner sees no toggle and no digest', async () => {
    renderApp('/', {
      viewer: 'p_tom',
      setup: (s) => {
        // The owner turned the preview on; Tom (not an owner) looks.
        s.setViewer('p_sev')
        seeded(s)
        s.setViewer('p_tom')
      },
    })
    await screen.findByRole('heading', { level: 1, name: 'Today' }, T)
    expect(screen.queryByRole('button', { name: 'Preview: mandates' })).toBeNull()
    expect(screen.queryByTestId('decided-for-you')).toBeNull()
  })
})

describe('the preview never signs', () => {
  it('a full run (on, issue, digest, stop, revoke) posts only to the preview endpoint: no sign or decision call', async () => {
    const spies = SIGNING.map((m) => vi.spyOn(api, m))
    const preview = vi.spyOn(api, 'postMandatesPreview')
    const { user } = renderApp('/agents?tab=mandates')
    await user.click(await screen.findByRole('switch', { name: /Show the pilot anyway/ }, T))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Issue a pilot mandate…' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Issue a pilot mandate…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Issue a pilot mandate' }, T)
    await user.click(within(dialog).getByRole('button', { name: /Sign mandate/ }))
    await screen.findByTestId('mandate-banner', {}, T)
    await user.click(within(screen.getByTestId('mandate-banner')).getByRole('button', { name: 'Stop' }))
    await user.click(within(await screen.findByRole('dialog', { name: /Stop mandate/ }, T)).getByRole('button', { name: /Stop mandate/ }))
    await waitFor(() => expect(screen.getByTestId('mandate-banner')).toHaveTextContent('Stopped at #1842'), T)
    await user.click(screen.getByRole('button', { name: 'Revoke and void…' }))
    await user.click(within(await screen.findByRole('alertdialog', {}, T)).getByRole('button', { name: /Revoke and void/ }))
    await waitFor(() => expect(banner()).toBeNull())
    for (const s of spies) expect(s, s.getMockName()).not.toHaveBeenCalled()
    const ops = preview.mock.calls.map((c) => c[1].op)
    expect(ops).toEqual(['enable', 'issue', 'stop', 'revoke'])
    for (const c of preview.mock.calls) expect(JSON.stringify(c[1])).not.toMatch(/"confirm"|sign/)
  })
  it('the digest actions post only to the preview endpoint too', async () => {
    const spies = SIGNING.map((m) => vi.spyOn(api, m))
    const preview = vi.spyOn(api, 'postMandatesPreview')
    const { user } = renderApp('/', { setup: seeded })
    const digest = await screen.findByTestId('decided-for-you', {}, T)
    const first = () => within(digest).getAllByTestId(/^mandate-decision:/)[0]
    await user.click(within(first()).getByRole('button', { name: 'Looks right' }))
    await waitFor(() => expect(within(digest).getAllByTestId(/^mandate-decision:/)).toHaveLength(4))
    await user.click(within(first()).getByRole('button', { name: 'Veto' }))
    await waitFor(() => expect(within(digest).getAllByTestId(/^mandate-decision:/)).toHaveLength(3))
    for (const s of spies) expect(s).not.toHaveBeenCalled()
    expect(preview.mock.calls.map((c) => c[1].op)).toEqual(['review', 'review'])
  })
})
