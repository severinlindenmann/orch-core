import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { MockStore } from '@/mocks/store'
import { installAndGrant } from '@/test/installAddon'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 6000 }
const on = (s: MockStore) => installAndGrant(s, s.workspaces.find((w) => w.prefix === 'DEMO')!.id, 'links')
const PATH = '/addon/links/links'

describe('Workspace links page (Preview)', () => {
  it('shows one A, a Preview chip, the tabs and the links with their carrier', async () => {
    renderApp(PATH, { viewer: 'p_sev', setup: on })
    const h1 = await screen.findByRole('heading', { level: 1, name: /Workspace links/ }, T)
    expect(within(h1).getByText('Preview')).toBeInTheDocument()
    for (const name of [/^Links/, /^Requests/, /^Log/, /^Set up/]) expect(screen.getByRole('tab', { name })).toBeInTheDocument()
    const row = (await screen.findByText('INT · Internal', {}, T)).closest('tr')!
    expect(within(row).getByText('Same machine')).toBeInTheDocument()
    expect(within(row).getByText('active')).toBeInTheDocument()
    expect(screen.getByText('Relay · off: messages wait')).toBeInTheDocument()
    expect(within(screen.getByRole('main')).getAllByRole('img', { name: /From (the Workspace links addon|addon: links)/ })).toHaveLength(1)
  })
  it('Details shows the selected link and its latest log entries', async () => {
    const { user } = renderApp(PATH, { viewer: 'p_sev', setup: on })
    const row = (await screen.findByText('Northwind Grid · OPS', {}, T)).closest('tr')!
    await user.click(within(row).getByRole('button', { name: 'Details' }))
    expect(await screen.findByText('Lena Brandt, Northwind Grid AG', {}, T)).toBeInTheDocument()
    expect(screen.getByText(/Dropped tariff-q3.xlsx to Northwind/)).toBeInTheDocument()
  })
  it('Requests draws the open requests as core decisions', async () => {
    const { user } = renderApp(PATH, { viewer: 'p_sev', setup: on })
    await user.click(await screen.findByRole('tab', { name: /^Requests/ }, T))
    expect(await screen.findByRole('button', { name: 'Codes match: link on these terms' }, T)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Accept into Backlog' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Review handoff' })).toBeInTheDocument()
  })
  it('the terms form sits above the pairing decision; accepting is off while it holds unsaved changes', async () => {
    const { user } = renderApp(PATH, { viewer: 'p_sev', setup: on })
    await user.click(await screen.findByRole('tab', { name: /^Requests/ }, T))
    const accept = await screen.findByRole('button', { name: 'Codes match: link on these terms' }, T)
    const box = screen.getByRole('checkbox', { name: 'They may send us Drop files' })
    expect(box.compareDocumentPosition(accept) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(accept).toBeEnabled()
    await user.click(box)
    await waitFor(() => expect(accept).toBeDisabled(), T)
    // The edit stays on screen (core keeps a guarded form's draft while it shows "Unsaved changes").
    expect(screen.getByRole('checkbox', { name: 'They may send us Drop files' })).not.toBeChecked()
    // Only the decision whose terms the form sets is held; Deny and the other decisions stay usable.
    expect(screen.getByText('Unsaved changes to the terms: save or cancel them before you accept.')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Deny' })[0]).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Accept into Backlog' })).toBeEnabled()
    await user.click(screen.getByRole('button', { name: 'Cancel changes' }))
    // Core asks before the cancel discards the edits.
    await user.click(await screen.findByRole('button', { name: 'Discard changes' }, T))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Codes match: link on these terms' })).toBeEnabled(), T)
    expect(screen.getByRole('checkbox', { name: 'They may send us Drop files' })).toBeChecked()
  })
  it('Set up: an owner gets the pairing form; a maintainer reads that owners set up links', async () => {
    const owner = renderApp(PATH, { viewer: 'p_sev', setup: on })
    await owner.user.click(await screen.findByRole('tab', { name: /^Set up/ }, T))
    expect(await screen.findByRole('button', { name: 'Start pairing' }, T)).toBeInTheDocument()
    expect(screen.getByText(/no direct network path \(D11\)/)).toBeInTheDocument()
    owner.unmount()
    const m = renderApp(PATH, { viewer: 'p_mara', setup: on })
    await m.user.click(await screen.findByRole('tab', { name: /^Set up/ }, T))
    expect(await screen.findByText('Owners set up links', {}, T)).toBeInTheDocument()
  })
  it('Sign and send goes through end to end (the signed args reach the host intact)', async () => {
    const { user } = renderApp(PATH, {
      viewer: 'p_sev',
      setup: (s) => {
        on(s)
        s.addonState(s.workspaces.find((w) => w.prefix === 'DEMO')!.id, 'links').nav = { p_sev: { draft: { link: 'ln_int', ticket: 'DEMO-0045' } } }
      },
    })
    await user.click(await screen.findByRole('tab', { name: /^Requests/ }, T))
    await user.click(await screen.findByRole('button', { name: 'Sign and send' }, T))
    await user.click(within(await screen.findByRole('dialog', {}, T)).getByRole('button', { name: 'Sign and run' }))
    expect(await screen.findByText(/Handed off DEMO-0045 to INT/, {}, T)).toBeInTheDocument()
  })
  it('a viewer reads the log without the restricted ticket and cannot revoke', async () => {
    const { user } = renderApp(PATH, { viewer: 'p_tom', setup: on })
    const row = (await screen.findByText('INT · Internal', {}, T)).closest('tr')!
    expect(within(row).queryByRole('button', { name: 'Revoke' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: /^Log/ }))
    await waitFor(() => expect(screen.getByText(/Handed off DEMO-0042 to INT/)).toBeInTheDocument(), T)
    expect(screen.queryByText(/DEMO-0044/)).not.toBeInTheDocument()
  })
})
