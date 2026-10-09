import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { MockStore } from '@/mocks/store'
import { installAndGrant } from '@/test/installAddon'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 4000 }
const on = (s: MockStore) => installAndGrant(s, s.workspaces.find((w) => w.prefix === 'DEMO')!.id, 'drop')

describe('Drop page (Preview)', () => {
  it('shows one A, a Preview chip, the simulated notice, the inbox, sent drops and the share form', async () => {
    renderApp('/addon/drop/drop', { viewer: 'p_sev', setup: on })
    const h1 = await screen.findByRole('heading', { level: 1, name: /Drop/ }, T)
    expect(within(h1).getByText('Preview')).toBeInTheDocument()
    expect(await screen.findByText('meter-room-photos.zip', {}, T)).toBeInTheDocument()
    expect(screen.getByText(/nothing leaves this machine/)).toBeInTheDocument()
    expect(screen.getByText('tariff-source-diff.csv')).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: 'Share file' }, T)).toBeInTheDocument()
    expect(within(screen.getByRole('main')).getAllByRole('img', { name: /From (the Drop addon|addon: drop)/ })).toHaveLength(1)
  })
  it('Claim takes a to-claim file', async () => {
    const { user } = renderApp('/addon/drop/drop', { viewer: 'p_sev', setup: on })
    const row = (await screen.findByText('release-notes.md', {}, T)).closest('tr')!
    await user.click(within(row).getByRole('button', { name: 'Claim' }))
    await waitFor(() => expect(within(screen.getByText('release-notes.md').closest('tr')!).getByText('claimed here')).toBeInTheDocument(), T)
  })
  it('a link is shown once in core\'s dialog', async () => {
    const { user } = renderApp('/addon/drop/drop', { viewer: 'p_sev', setup: on })
    await screen.findByRole('button', { name: 'Share file' }, T)
    await user.selectOptions(screen.getByRole('combobox', { name: /^File/ }), 'release-notes.md')
    await user.selectOptions(screen.getByRole('combobox', { name: /^Send to/ }), 'link')
    await user.click(screen.getByRole('button', { name: 'Share file' }))
    const dialog = await screen.findByRole('dialog', { name: /Copy this link now/ }, T)
    expect((within(dialog).getByRole('textbox') as HTMLInputElement).value).toMatch(/^https:\/\/relay\.dev\.severin\.io\/d\//)
    expect(within(dialog).getByText(/shown once/)).toBeInTheDocument()
  })
  it('a viewer reads but cannot claim or share', async () => {
    renderApp('/addon/drop/drop', { viewer: 'p_tom', setup: on })
    const row = (await screen.findByText('release-notes.md', {}, T)).closest('tr')!
    await waitFor(() => expect(within(row).getByRole('button', { name: 'Claim' })).toBeDisabled(), T)
    expect(screen.queryByText('incident-timeline.md')).not.toBeInTheDocument()
  })
})
