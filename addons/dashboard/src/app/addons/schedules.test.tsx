import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
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
    expect(screen.getAllByText('Weekly dependency update').length).toBeGreaterThan(0)
    await user.click(screen.getByRole('button', { name: 'Expand sidebar' }))
    await user.click(await screen.findByRole('button', { name: /More addons/ }))
    expect(within(await screen.findByRole('link', { name: /^Schedules/ })).getByText('Preview')).toBeInTheDocument()
  })
  it('Arm is signed in core\'s dialog and then shows the next run', async () => {
    const { user } = renderApp('/addon/schedules/schedules', { viewer: 'p_sev', setup: on })
    await screen.findByText('Smoke test on testing', {}, T)
    await user.click(within(itemOf('Smoke test on testing')).getByRole('button', { name: 'Arm' }))
    const dialog = await screen.findByRole('dialog', { name: /Sign: arm · Schedules/ }, T)
    // The row's own name, labelled as the addon's words; the signed args stay visible next to it.
    expect(within(dialog).getByText(/Addon says:/)).toHaveTextContent('Smoke test on testing')
    expect(within(dialog).getByText(/id = smoke-on-testing/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: /Sign and run/ }))
    await waitFor(() => expect(within(itemOf('Smoke test on testing')).getByText(/on the next ticket moved to testing/)).toBeInTheDocument(), T)
    await user.click(within(itemOf('Smoke test on testing')).getByRole('button', { name: /^More actions for / }))
    expect(await screen.findByRole('menuitem', { name: 'Disarm' })).toBeInTheDocument()
  })
  it('a finding is filed in place: File ticket in backlog is on the page, and nothing says from Today', async () => {
    const { user } = renderApp('/addon/schedules/schedules', { viewer: 'p_sev', setup: on })
    const file = await screen.findByRole('button', { name: /File ticket/ }, T)
    expect(screen.queryByText(/from Today/)).not.toBeInTheDocument()
    await user.click(file)
    await user.click(await screen.findByRole('button', { name: 'Send answer' }, T))
    await waitFor(() => expect(mockStore.listTickets(wsOf(mockStore)).some((t) => t.title === 'Update dependencies, week 41')).toBe(true), T)
  })
  it('a refused Arm shows in the row, not in a toast', async () => {
    const error = vi.spyOn(toast, 'error')
    vi.spyOn(api, 'runAddonAction').mockRejectedValue(new ApiError(409, { code: 'schedule.x', message: 'Smoke test on testing cannot be armed now.', hint: 'Try again later.', retryable: false }))
    const { user } = renderApp('/addon/schedules/schedules', { viewer: 'p_sev', setup: on })
    await screen.findByText('Smoke test on testing', {}, T)
    await user.click(within(itemOf('Smoke test on testing')).getByRole('button', { name: 'Arm' }))
    await user.click(within(await screen.findByRole('dialog', { name: /Sign: arm/ }, T)).getByRole('button', { name: /Sign and run/ }))
    const alert = await within(itemOf('Smoke test on testing')).findByRole('alert', {}, T)
    expect(alert).toHaveTextContent(/cannot be armed now\. Try again later/)
    expect(error).not.toHaveBeenCalled()
    vi.restoreAllMocks()
  })
  it('a later successful Arm clears the row\'s earlier error', async () => {
    const post = vi.spyOn(api, 'runAddonAction')
    post.mockRejectedValueOnce(new ApiError(409, { code: 'x', message: 'Not now.', retryable: false }))
    post.mockResolvedValueOnce({ ok: true, message: 'Armed.' })
    const { user } = renderApp('/addon/schedules/schedules', { viewer: 'p_sev', setup: on })
    await screen.findByText('Smoke test on testing', {}, T)
    const arm = async () => {
      await user.click(within(itemOf('Smoke test on testing')).getByRole('button', { name: 'Arm' }))
      await user.click(within(await screen.findByRole('dialog', { name: /Sign: arm/ }, T)).getByRole('button', { name: /Sign and run/ }))
    }
    await arm()
    await within(itemOf('Smoke test on testing')).findByRole('alert', {}, T)
    await arm()
    await waitFor(() => expect(within(itemOf('Smoke test on testing')).queryByRole('alert')).not.toBeInTheDocument(), T)
    vi.restoreAllMocks()
  })
  it('a secret from a signed action opens the Copy this link now dialog', async () => {
    vi.spyOn(api, 'runAddonAction').mockResolvedValue({ ok: true, message: 'Armed.', secret: { label: 'Link', value: 'https://p.acme.example/s/abc' } })
    const { user } = renderApp('/addon/schedules/schedules', { viewer: 'p_sev', setup: on })
    await screen.findByText('Smoke test on testing', {}, T)
    await user.click(within(itemOf('Smoke test on testing')).getByRole('button', { name: 'Arm' }))
    await user.click(within(await screen.findByRole('dialog', { name: /Sign: arm/ }, T)).getByRole('button', { name: /Sign and run/ }))
    expect(await screen.findByRole('dialog', { name: /Copy this link now/ }, T)).toBeInTheDocument()
    vi.restoreAllMocks()
  })
  it('a refused in-place decision shows under the row, not in a toast', async () => {
    const error = vi.spyOn(toast, 'error')
    const real = api.runAddonAction.bind(api)
    vi.spyOn(api, 'runAddonAction').mockImplementation((ws, addon, action, body) =>
      action === 'finding' ? Promise.reject(new ApiError(409, { code: 'decision.closed', message: 'That decision is closed.', retryable: false })) : real(ws, addon, action, body),
    )
    const { user } = renderApp('/addon/schedules/schedules', { viewer: 'p_sev', setup: on })
    await user.click(await screen.findByRole('button', { name: /File ticket/ }, T))
    await user.click(await screen.findByRole('button', { name: 'Send answer' }, T))
    const alert = await screen.findByRole('alert', {}, T)
    expect(alert).toHaveTextContent('That decision is closed.')
    expect(error).not.toHaveBeenCalled()
    vi.restoreAllMocks()
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
    const q = (await screen.findAllByText('Dependency update · Monday: file it?', {}, T))[0]
    const card = q.closest('[data-testid^="card-addon:"]') as HTMLElement
    await user.click(within(card).getByRole('button', { name: 'Decide' }))
    await user.click(within(card).getByRole('button', { name: 'File ticket in backlog' }))
    await user.click(await screen.findByRole('button', { name: 'Send answer' }, T))
    await waitFor(() => expect(mockStore.listTickets(wsOf(mockStore)).some((t) => t.title === 'Update dependencies, week 41' && t.status === 'backlog')).toBe(true), T)
    await waitFor(() => expect(screen.queryByText('Dependency update · Monday: file it?')).not.toBeInTheDocument(), T)
  })
})
