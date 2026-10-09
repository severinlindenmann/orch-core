import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { SCENARIOS, STEP_COUNT } from './scenarios'
import { TICKS_KEY } from './ReviewTour'

// The review tour: the "Demo data" pill opens a sheet with the REVIEW.md scenarios as checklists.
const T = { timeout: 8000 }

async function openTour(user: ReturnType<typeof renderApp>['user']) {
  await user.click(await screen.findByRole('button', { name: /^Review tour/ }, T))
  return screen.findByRole('dialog', { name: 'Review tour' }, T)
}

async function openScenario(user: ReturnType<typeof renderApp>['user'], sheet: HTMLElement, title: RegExp) {
  const head = within(sheet).getByRole('button', { name: title })
  if (head.getAttribute('aria-expanded') !== 'true') await user.click(head)
}

afterEach(() => {
  mockStore.sim.stopAll()
  vi.restoreAllMocks()
})

describe('review tour', () => {
  it('Go on "Owner admin · Install quick tasks" lands on Settings > Addons and closes the sheet', async () => {
    const { user } = renderApp('/')
    const sheet = await openTour(user)
    await openScenario(user, sheet, /^6\. Owner admin/)
    await user.click(within(sheet).getByRole('button', { name: 'Go: Owner admin · Install quick tasks' }))
    expect(await screen.findByRole('button', { name: 'Browse addons' }, T)).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Review tour' })).toBeNull())
    expect(screen.getByTestId('topbar-title')).toHaveTextContent('Settings')
  })

  it('Go switches the viewer when the step is for someone else, and back to Severin otherwise', async () => {
    const { user } = renderApp('/')
    let sheet = await openTour(user)
    await openScenario(user, sheet, /^4\. Maintainer/)
    await user.click(within(sheet).getByRole('button', { name: 'Go: Maintainer (Mara) · Settings are read-only' }))
    await waitFor(() => expect(mockStore.viewer).toBe('p_mara'))
    expect((await screen.findAllByText(/Only owners/, undefined, T)).length).toBeGreaterThan(0)
    sheet = await openTour(user)
    await openScenario(user, sheet, /^6\. Owner admin/)
    await user.click(within(sheet).getByRole('button', { name: 'Go: Owner admin · Change a gate policy' }))
    await waitFor(() => expect(mockStore.viewer).toBe('p_sev'))
  })

  it('an unsaved New ticket overlay holds Go: nothing changes until Discard is confirmed', async () => {
    const { user } = renderApp('/')
    await user.click(await screen.findByRole('button', { name: /New ticket/ }, T))
    const overlay = await screen.findByRole('dialog', { name: 'New ticket' }, T)
    await user.type(within(overlay).getByRole('textbox', { name: 'Quick ticket' }), 'Something to keep')
    // The overlay is modal, so the tour is opened and used without pointer checks.
    fireEvent.click(screen.getByRole('button', { name: /^Review tour/, hidden: true }))
    const sheet = await screen.findByRole('dialog', { name: 'Review tour' }, T)
    const head = within(sheet).getByRole('button', { name: /^4\. Maintainer/, hidden: true })
    if (head.getAttribute('aria-expanded') !== 'true') fireEvent.click(head)
    fireEvent.click(within(sheet).getByRole('button', { name: 'Go: Maintainer (Mara) · Settings are read-only', hidden: true }))
    expect(await screen.findByText('Discard unsaved changes?', undefined, T)).toBeInTheDocument()
    expect(mockStore.viewer).toBe('p_sev')
    await user.click(screen.getByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(mockStore.viewer).toBe('p_mara'), T)
    await waitFor(() => expect(screen.getByTestId('topbar-title')).toHaveTextContent('Settings'), T)
  })

  it('Go switches the workspace back to DEMO', async () => {
    const { user } = renderApp('/', { storage: { 'orch.workspace': mockStore.workspaces.find((w) => w.prefix === 'INT')!.id } })
    const sheet = await openTour(user)
    await openScenario(user, sheet, /^1\. Owner morning/)
    await user.click(within(sheet).getByRole('button', { name: 'Go: Owner morning · Move a card on the Board' }))
    const demo = mockStore.workspaces.find((w) => w.prefix === 'DEMO')!.id
    await waitFor(() => expect(localStorage.getItem('orch.workspace')).toBe(demo))
  })

  it('a step on the busy day asks before switching the demo data', async () => {
    const { user } = renderApp('/')
    const sheet = await openTour(user)
    await openScenario(user, sheet, /^11\. Epics/)
    await user.click(within(sheet).getByRole('button', { name: 'Go: Epics · Board grouped by epic' }))
    const ask = await screen.findByRole('alertdialog', undefined, T)
    expect(within(ask).getByText('This step uses the busy day demo')).toBeInTheDocument()
    await user.click(within(ask).getByRole('button', { name: 'Switch and go' }))
    await waitFor(() => expect(mockStore.dataset).toBe('busy'), T)
    await waitFor(() => expect(screen.getByTestId('topbar-title')).toHaveTextContent('Board'), T)
  })

  it('ticks are kept in this browser and counted', async () => {
    const first = renderApp('/')
    const sheet = await openTour(first.user)
    await openScenario(first.user, sheet, /^6\. Owner admin/)
    const box = within(sheet).getByRole('checkbox', { name: 'Install quick tasks' })
    await first.user.click(box)
    expect(box).toBeChecked()
    expect(JSON.parse(localStorage.getItem(TICKS_KEY)!)).toEqual(['6.4'])
    expect(within(sheet).getByText(`1 of ${STEP_COUNT} checked`)).toBeInTheDocument()
    expect(within(sheet).getByRole('button', { name: /^6\. Owner admin/ })).toHaveTextContent('1/6')
    expect(screen.getByRole('button', { name: /^Review tour/, hidden: true })).toHaveTextContent(`1/${STEP_COUNT}`)
  })

  it('works when browser storage is blocked', async () => {
    const { user } = renderApp('/')
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    const sheet = await openTour(user)
    await openScenario(user, sheet, /^2\. New work/)
    const box = within(sheet).getByRole('checkbox', { name: 'Create a bug' })
    await user.click(box)
    expect(box).toBeChecked()
  })

  it('"Reset demo data and ticks" asks, then clears both', async () => {
    const { user } = renderApp('/')
    const sheet = await openTour(user)
    await openScenario(user, sheet, /^1\. Owner morning/)
    await user.click(within(sheet).getByRole('checkbox', { name: 'Read what needs you' }))
    mockStore.append('DEMO-0043', { type: 'log.added', actor: 'p_sev', text: 'a review change' })
    await user.click(within(sheet).getByRole('button', { name: 'Reset demo data and ticks' }))
    const ask = await screen.findByRole('alertdialog', undefined, T)
    await user.click(within(ask).getByRole('button', { name: 'Reset' }))
    await waitFor(() => expect(mockStore.eventsOf('DEMO-0043').some((e) => e.text === 'a review change')).toBe(false))
    await waitFor(() => expect(within(sheet).getByRole('checkbox', { name: 'Read what needs you' })).not.toBeChecked())
    expect(localStorage.getItem(TICKS_KEY)).toBe('[]')
  })
})

describe('REVIEW.md and the tour list the same scenarios', () => {
  it('every scenario and step of the tour is in REVIEW.md, in order', () => {
    const md = readFileSync(resolve(process.cwd(), 'REVIEW.md'), 'utf8')
    const part = md.slice(md.indexOf('## Scenarios'), md.indexOf('\n## ', md.indexOf('## Scenarios') + 1))
    const heads = [...part.matchAll(/^### (\d+)\. (.+)$/gm)].map((m) => `${m[1]}. ${m[2]}`)
    expect(heads).toEqual(SCENARIOS.map((s) => `${s.n}. ${s.title}`))
    const steps = [...part.matchAll(/^- \[ \] \*\*(.+?)\*\*/gm)].map((m) => m[1])
    expect(steps).toEqual(SCENARIOS.flatMap((s) => s.steps.map((st) => st.title)))
  })
})
