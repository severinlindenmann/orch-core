// Mandates, PREVIEW ONLY (M1): Agents → Mandates, the shell banner, Today's "Decided for you", the demo toggle, and
// the guarantee that the preview never reaches a signing or decision path.
import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { PREVIEW_LINE } from '@/api/mandatesPreview'
import { ALWAYS_HUMAN, MAY, PROTECTED_PATHS } from '@/mocks/mandates-preview'
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

/** Every api method that signs or decides something real: the preview must never call one (listed for the record). */
const SIGNING = ['postAction', 'runAddonAction', 'issueGrant', 'revokeGrant', 'grantSkillCredentials', 'postAddonOp', 'postRelay', 'postSettings'] as const
/** Spies on every api method; `writes()` names each called method that is not a read (get… / list…). */
function spyAll() {
  const spies = (Object.keys(api) as (keyof typeof api)[]).map((k) => [k, vi.spyOn(api, k)] as const)
  return {
    writes: () => spies.filter(([k, sp]) => sp.mock.calls.length > 0 && !/^(get|list)/.test(k)).map(([k]) => k),
    signing: () => spies.filter(([k, sp]) => (SIGNING as readonly string[]).includes(k) && sp.mock.calls.length > 0).map(([k]) => k),
  }
}
/** Opens the folded digest on Today. */
async function openDigest(user: ReturnType<typeof renderApp>['user']) {
  const digest = await screen.findByTestId('decided-for-you', {}, T)
  await user.click(within(digest).getByRole('button', { name: /^Decided for you/ }))
  return digest
}

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

  it('the preflight blocks issuing until "Show the mandate anyway"', async () => {
    const { user } = renderApp('/agents?tab=mandates')
    await screen.findByTestId('mandates-tab', {}, T)
    for (const id of ['p1', 'p2', 'p3', 'p4']) expect(screen.getByTestId(`preflight-${id}`)).toHaveTextContent(/not available in this build/)
    expect(screen.getByTestId('preflight-p1')).toHaveTextContent(/P2 custody/)
    expect(screen.getByTestId('preflight-blocked')).toHaveTextContent(/Issuing is blocked/)
    const issue = screen.getByRole('button', { name: 'Issue a mandate…' })
    expect(issue).toBeDisabled()
    expect(screen.getByTestId('issue-blocked')).toBeInTheDocument()
    const anyway = screen.getByRole('switch', { name: /Show the mandate anyway \(preview\)/ })
    expect(anyway).toHaveAttribute('aria-checked', 'false')
    await user.click(anyway)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Issue a mandate…' })).toBeEnabled())
    // Turning the preview on this way issues nothing: no banner yet.
    expect(banner()).toBeNull()
  })

  it('the issue dialog: steers the whole workspace, the may-do and always-yours lists and protected paths in full, a length up to 30 days; signing it signs nothing', async () => {
    const { user } = renderApp('/agents?tab=mandates', { setup: (s) => void s.mandatesPreview.request(wsOf(s), { op: 'enable' }) })
    await user.click(await screen.findByRole('button', { name: 'Issue a mandate…' }, T))
    const dialog = await screen.findByRole('dialog', { name: 'Issue a mandate' }, T)
    expect(within(dialog).getByText(PREVIEW_LINE)).toBeInTheDocument()
    const covers = within(dialog).getByTestId('preview-covers')
    expect(covers).toHaveTextContent('Steers the whole workspace in your name')
    expect(covers).toHaveTextContent('Always yours: settings, addons, members and roles, devices, relay pairing, secrets and connections, protected paths')
    expect(covers).toHaveTextContent('It never issues another mandate')
    expect(covers).toHaveTextContent(/Duration: 7 days, until .* UTC · renewable with one new signature/)
    expect(covers).toHaveTextContent(/Orchestrator: /)
    expect(dialog).not.toHaveTextContent(/epic|pilot/i)
    // Only the orchestrator and the length are inputs; the length goes up to 30 days.
    expect(within(dialog).getAllByRole('combobox').map((c) => c.id)).toEqual(['mandate-orch', 'mandate-days'])
    expect(within(within(dialog).getByLabelText('Length')).getAllByRole('option').map((o) => o.getAttribute('value'))).toEqual(['1', '3', '7', '14', '30'])
    await user.selectOptions(within(dialog).getByLabelText('Length'), '30')
    expect(covers).toHaveTextContent(/Duration: 30 days, until/)
    expect(within(dialog).queryByRole('textbox')).toBeNull()
    const may = within(within(dialog).getByTestId('mandate-may')).getAllByRole('listitem').map((li) => li.textContent)
    expect(may).toEqual(MAY)
    expect(may.join(' ')).toMatch(/approve agents that wait for an approval/i)
    expect(may.join(' ')).toMatch(/Unblock tickets/)
    expect(may.join(' ')).toContain('Enable factories and start factory runs, including Deliver (after the hold window).')
    expect(within(within(dialog).getByTestId('mandate-always-human')).getAllByRole('listitem').map((li) => li.textContent)).toEqual(ALWAYS_HUMAN)
    expect(within(within(dialog).getByTestId('mandate-protected')).getAllByRole('listitem').map((li) => li.textContent)).toEqual(PROTECTED_PATHS)
    await user.click(within(dialog).getByRole('button', { name: 'Sign mandate (preview — nothing is signed)' }))
    expect(await screen.findByTestId('mandate-log', {}, T)).toBeInTheDocument()
    expect(await screen.findByTestId('mandate-banner', {}, T)).toHaveTextContent(/Mandate md_3 · for Severin · whole workspace · 10 decisions · until /)
  })

  it('the mandate in force: limits as meters, the decision log (a mix) in core words, refused items with reasons, revisions', async () => {
    renderApp('/agents?tab=mandates', { setup: seeded })
    const log = await screen.findByTestId('mandate-log', {}, T)
    const rows = within(log).getAllByRole('listitem')
    expect(rows).toHaveLength(10)
    for (const kind of ['Code review approved', 'Ticket unblocked', 'Factory enabled', 'Factory run started', 'Factory permit granted', 'Grant issued (agent started)', 'Requirements approved'])
      expect(log).toHaveTextContent(`${kind}: via mandate md_3, for Severin — no person reviewed this`)
    expect(log).toHaveTextContent(/Verdict: via mandate md_3, for Severin — no person reviewed this \(commit b7e1f02\)/)
    expect(log).toHaveTextContent(/checked by checker si_chk_4f2a \(passed\)/)
    const limits = screen.getByTestId('mandate-limits')
    expect(within(limits).getByRole('progressbar', { name: 'Decisions' })).toHaveAttribute('aria-valuenow', '10')
    expect(within(limits).getByRole('progressbar', { name: 'Days left' })).toHaveAttribute('aria-valuemax', '7')
    expect(within(limits).getByRole('progressbar', { name: 'Grants issued' })).toHaveAttribute('aria-valuenow', '1')
    const refused = screen.getByTestId('mandate-refused')
    expect(refused).toHaveTextContent('Protected path')
    expect(refused).toHaveTextContent('Your veto')
    expect(refused).toHaveTextContent('Always yours')
    expect(refused).toHaveTextContent(/No mandate chains.*mandate\.chain_refused/)
    expect(refused).not.toHaveTextContent('Limit reached')
    expect(within(refused).getAllByRole('listitem')).toHaveLength(4)
    expect(screen.getByText(/Steers the whole workspace in your name, except what stays yours/)).toBeInTheDocument()
    expect(screen.getByText(/Revision 1/)).toBeInTheDocument()
  })

  it('Revoke and void lists exactly the decisions on work not landed, and the landed ones for review', async () => {
    const { user } = renderApp('/agents?tab=mandates', { setup: seeded })
    await user.click(await screen.findByRole('button', { name: 'Revoke and void…' }, T))
    const dialog = await screen.findByRole('alertdialog', {}, T)
    expect(dialog).toHaveTextContent('Revoke mandate md_3 and void its decisions?')
    const voids = within(within(dialog).getByTestId('revoke-voids')).getAllByRole('listitem').map((li) => li.textContent!.slice(0, 14))
    // Seeded: #1831–#1840, the verdicts #1833 and #1835 landed; workspace-level decisions read "workspace".
    expect(voids.map((v) => v.slice(0, 5))).toEqual(['#1831', '#1832', '#1834', '#1836', '#1837', '#1838', '#1839', '#1840'])
    expect(voids[0]).toBe('#1831DEMO-0051')
    expect(voids[4]).toBe('#1837workspace')
    const landed = within(within(dialog).getByTestId('revoke-landed')).getAllByRole('listitem').map((li) => li.textContent!.slice(0, 5))
    expect(landed).toEqual(['#1833', '#1835'])
    expect(within(dialog).getByTestId('preview-covers')).toHaveTextContent('Voids 8 decisions on work that has not landed')
    await user.click(within(dialog).getByRole('button', { name: 'Revoke and void (preview — nothing is signed)' }))
    expect(await screen.findByText(/Revoked .*: 8 decisions voided, 2 on landed work listed for your review/, {}, T)).toBeInTheDocument()
    await waitFor(() => expect(banner()).toBeNull())
  })
})

describe('Renew (preview)', () => {
  it('Renew picks a new length (up to 30 days) from now: a new revision, same scope; nothing signs', async () => {
    const spy = spyAll()
    const { user } = renderApp('/agents?tab=mandates', { setup: seeded })
    await user.click(await screen.findByRole('button', { name: 'Renew…' }, T))
    const dialog = await screen.findByRole('dialog', { name: 'Renew mandate md_3' }, T)
    expect(within(dialog).getByTestId('preview-covers')).toHaveTextContent(/revision 2/)
    await user.selectOptions(within(dialog).getByLabelText('Length'), '30')
    expect(within(dialog).getByTestId('preview-covers')).toHaveTextContent(/New end: 30 days from now, until /)
    await user.click(within(dialog).getByRole('button', { name: 'Renew mandate (preview — nothing is signed)' }))
    expect(await screen.findByText(/revision 2/, { selector: 'dd' }, T)).toHaveTextContent(/30 days · renewable · revision 2/)
    expect(screen.getByText(/Revision 2/)).toBeInTheDocument()
    expect(spy.signing()).toEqual([])
    expect(spy.writes()).toEqual(['postMandatesPreview'])
  })
})

describe('shell banner (preview)', () => {
  it('appears with the preview on, is calm and one line, hides for the session, and goes when the preview is turned off', async () => {
    const { user } = renderApp('/board')
    await screen.findByRole('heading', { level: 1, name: /Board/ }, T)
    expect(banner()).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Preview: mandates' }))
    const b = await screen.findByTestId('mandate-banner', {}, T)
    expect(b).toHaveTextContent(/Mandate md_3 · for Severin · whole workspace · 10 decisions · until /)
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
    await user.click(within(b).getByRole('button', { name: 'Stop…' }))
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
  it('sits below the real queue, folded to one row; open: at most 3 decisions with Looks right and Veto, the refused items, Show all', async () => {
    const { user } = renderApp('/', { setup: seeded })
    const digest = await screen.findByTestId('decided-for-you', {}, T)
    // Below the real "needs you" groups (the queue section's last child), folded.
    const queue = screen.getByRole('region', { name: 'Needs you' })
    const groups = within(queue).getAllByRole('region').filter((r) => r !== digest)
    expect(groups.length).toBeGreaterThan(0)
    for (const g of groups) expect(g.compareDocumentPosition(digest) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    const toggle = within(digest).getByRole('button', { name: /^Decided for you/ })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(toggle).toHaveTextContent(/Decided for you · 5 since you last looked · 4 refused or skipped · Review/)
    expect(within(digest).queryAllByTestId(/^mandate-decision:/)).toHaveLength(0)
    await user.click(toggle)
    expect(digest).toHaveTextContent(PREVIEW_LINE)
    const rows = () => within(digest).queryAllByTestId(/^mandate-decision:/)
    expect(rows()).toHaveLength(3)
    expect(rows()[0]).toHaveTextContent(/^workspaceGrant issued \(agent started\): via mandate md_3, for Severin — no person reviewed this/)
    // No per-row revoke: one mandate-wide button in the header.
    expect(within(rows()[0]).queryByRole('button', { name: /Revoke/ })).toBeNull()
    expect(within(digest).getByRole('link', { name: /Show all \(2 more\) on Agents → Mandates/ }).getAttribute('href')).toMatch(/\/agents\?tab=mandates$/)
    await user.click(within(rows()[0]).getByRole('button', { name: 'Looks right: workspace grant issued (agent started)' }))
    await waitFor(() => expect(toggle).toHaveTextContent('4 since you last looked'))
    await user.click(within(rows()[0]).getByRole('button', { name: /^Veto: / }))
    await waitFor(() => expect(toggle).toHaveTextContent('3 since you last looked'))
    const refused = screen.getByTestId('mandate-refused-today')
    expect(within(refused).getAllByTestId(/^mandate-refused:/)).toHaveLength(4)
    expect(refused).toHaveTextContent('Issue a mandate to a second orchestrator')
    // Core's reason in full.
    expect(refused).toHaveTextContent('Protected path:Verdict refused: the diff touches package-lock.json (a dependency lockfile, protected). A person approves this.')
    await user.click(within(digest).getByRole('button', { name: 'Revoke mandate and void…' }))
    const dialog = await screen.findByRole('alertdialog', {}, T)
    expect(within(within(dialog).getByTestId('revoke-voids')).getAllByRole('listitem')).toHaveLength(8)
    await user.click(within(dialog).getByRole('button', { name: 'Revoke and void (preview — nothing is signed)' }))
    await waitFor(() => expect(screen.queryByTestId('decided-for-you')).toBeNull())
  })
})

describe('values through visible.tsx (preview dialogs)', () => {
  it('an issuer name with a bidi override is shown escaped in the banner, the digest and the Revoke dialog', async () => {
    const { user } = renderApp('/', {
      setup: (s) => {
        s.workspaces.find((w) => w.prefix === 'DEMO')!.members.find((m) => m.person === 'p_sev')!.name = 'Sev\u202Erin'
        seeded(s)
      },
    })
    const b = await screen.findByTestId('mandate-banner', {}, T)
    expect(b.textContent).toContain('Sev\\u{202e}rin')
    expect(b.textContent).not.toContain('\u202E')
    const digest = await openDigest(user)
    expect(digest.textContent).toContain('for Sev\\u{202e}rin — no person reviewed this')
    expect(digest.textContent).not.toContain('\u202E')
    await user.click(within(digest).getByRole('button', { name: 'Revoke mandate and void…' }))
    const dialog = await screen.findByRole('alertdialog', {}, T)
    expect(dialog.textContent).toContain('Sev\\u{202e}rin')
    expect(dialog.textContent).not.toContain('\u202E')
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
  it('a full run (on, issue, stop, revoke) posts only to the preview endpoint: no other write at all', async () => {
    const spy = spyAll()
    const preview = vi.spyOn(api, 'postMandatesPreview')
    const { user } = renderApp('/agents?tab=mandates')
    await user.click(await screen.findByRole('switch', { name: /Show the mandate anyway/ }, T))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Issue a mandate…' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Issue a mandate…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Issue a mandate' }, T)
    await user.click(within(dialog).getByRole('button', { name: /Sign mandate/ }))
    await screen.findByTestId('mandate-banner', {}, T)
    await user.click(within(screen.getByTestId('mandate-banner')).getByRole('button', { name: 'Stop…' }))
    await user.click(within(await screen.findByRole('dialog', { name: /Stop mandate/ }, T)).getByRole('button', { name: /Stop mandate/ }))
    await waitFor(() => expect(screen.getByTestId('mandate-banner')).toHaveTextContent('Stopped at #1842'), T)
    await user.click(screen.getByRole('button', { name: 'Revoke and void…' }))
    await user.click(within(await screen.findByRole('alertdialog', {}, T)).getByRole('button', { name: /Revoke and void/ }))
    await waitFor(() => expect(banner()).toBeNull())
    expect(spy.signing()).toEqual([])
    expect(spy.writes()).toEqual(['postMandatesPreview'])
    expect(preview.mock.calls.map((c) => c[1].op)).toEqual(['enable', 'issue', 'stop', 'revoke'])
    for (const c of preview.mock.calls) expect(JSON.stringify(c[1])).not.toMatch(/"confirm"|sign/)
  })
  it('the digest actions post only to the preview endpoint too', async () => {
    const spy = spyAll()
    const { user } = renderApp('/', { setup: seeded })
    const digest = await openDigest(user)
    const toggle = within(digest).getByRole('button', { name: /^Decided for you/ })
    const first = () => within(digest).getAllByTestId(/^mandate-decision:/)[0]
    await user.click(within(first()).getByRole('button', { name: /^Looks right/ }))
    await waitFor(() => expect(toggle).toHaveTextContent('4 since'))
    await user.click(within(first()).getByRole('button', { name: /^Veto/ }))
    await waitFor(() => expect(toggle).toHaveTextContent('3 since'))
    expect(spy.signing()).toEqual([])
    expect(spy.writes()).toEqual(['postMandatesPreview'])
  })
})
