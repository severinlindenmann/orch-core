import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import type { MockStore } from '@/mocks/store'
import { installAndGrant } from '@/test/installAddon'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 4000 }
const wsOf = (s: MockStore) => s.workspaces.find((w) => w.prefix === 'DEMO')!.id
const on = (s: MockStore) => installAndGrant(s, wsOf(s), 'schedules')
const itemOf = (text: string) => screen.getByText(text).closest('li')!

describe('Schedules page', () => {
  it('lists the three schedules with a Preview chip on the title and the nav entry', async () => {
    const { user } = renderApp('/addon/schedules/schedules', { viewer: 'p_sev', setup: on })
    const h1 = await screen.findByRole('heading', { level: 1, name: /Schedules/ }, T)
    expect(within(h1).getByText('Preview')).toBeInTheDocument()
    expect(await screen.findByText('Check inbox', {}, T)).toBeInTheDocument()
    expect(screen.getByText('Smoke test on testing')).toBeInTheDocument()
    expect(screen.getByText('Weekly dependency update')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Expand sidebar' }))
    expect(within(await screen.findByRole('link', { name: 'Schedules' })).getByText('Preview')).toBeInTheDocument()
  })
  it('Arm is signed in core\'s dialog and then shows the next run', async () => {
    const { user } = renderApp('/addon/schedules/schedules', { viewer: 'p_sev', setup: on })
    await screen.findByText('Smoke test on testing', {}, T)
    await user.click(within(itemOf('Smoke test on testing')).getByRole('button', { name: 'Arm' }))
    const dialog = await screen.findByRole('dialog', { name: /Sign: arm · Schedules/ }, T)
    expect(within(dialog).getByText(/smoke-on-testing/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: /Sign with Touch ID/ }))
    await waitFor(() => expect(within(itemOf('Smoke test on testing')).getByText(/on the next ticket moved to testing/)).toBeInTheDocument(), T)
    expect(within(itemOf('Smoke test on testing')).getByRole('button', { name: 'Disarm' })).toBeInTheDocument()
  })
  it('Run now adds a run and shows its report as markdown', async () => {
    const { user, container } = renderApp('/addon/schedules/schedules', { viewer: 'p_sev', setup: on })
    await screen.findByText('Check inbox', {}, T)
    await user.click(within(itemOf('Check inbox')).getByRole('button', { name: 'Run now' }))
    await waitFor(() => expect([...container.querySelectorAll('.addon-md')].some((e) => /mails/.test(e.textContent ?? ''))).toBe(true), T)
    expect(screen.getByRole('heading', { name: /Report: Check inbox/ })).toBeInTheDocument()
  })
  it('a viewer can read but not arm', async () => {
    renderApp('/addon/schedules/schedules', { viewer: 'p_tom', setup: on })
    await screen.findByText('Smoke test on testing', {}, T)
    await waitFor(() => expect(within(itemOf('Smoke test on testing')).getByRole('button', { name: 'Arm' })).toBeDisabled(), T)
  })
})

describe('the recurring finding on Today', () => {
  it('"Dependency update · Monday: file it?" files a backlog ticket', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev', setup: on })
    const q = await screen.findByText('Dependency update · Monday: file it?', {}, T)
    const card = q.closest('[data-testid^="card-addon:"]') as HTMLElement
    await user.click(within(card).getByRole('button', { name: 'File ticket in backlog' }))
    await user.click(await screen.findByRole('button', { name: 'Sign with Touch ID' }, T))
    await waitFor(() => expect(mockStore.listTickets(wsOf(mockStore)).some((t) => t.title === 'Update dependencies, week 41' && t.status === 'backlog')).toBe(true), T)
    await waitFor(() => expect(screen.queryByText('Dependency update · Monday: file it?')).not.toBeInTheDocument(), T)
  })
})
