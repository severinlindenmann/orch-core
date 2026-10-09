import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { mockStore } from '@/api/client'
import type { MockStore } from '@/mocks/store'
import { getAddon } from '@/mocks/addons/registry'
import { installAndGrant } from '@/test/installAddon'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 4000 }
const wsOf = (s: MockStore) => s.workspaces.find((w) => w.prefix === 'DEMO')!.id
const on = (s: MockStore) => installAndGrant(s, wsOf(s), 'models')
const code = (text: string) => (_: string, el: Element | null) => el?.tagName === 'CODE' && el.textContent === text
const SENTENCE = 'The Standard model "son net" is not a model name (no spaces, no leading "-"). Start is blocked until it is fixed in the Model routing settings.'
const invalid = (s: MockStore) => {
  on(s)
  s.runAddon(wsOf(s), 'models', 'save_settings', { formData: { ...(s.addonStateView(wsOf(s), 'models')!.settings as object), standard: 'son net' } })
}

describe('model routing in the start-agent panel', () => {
  it('shows the model line and --model in the command', async () => {
    renderApp('/ticket/DEMO-0044', { viewer: 'p_sev', setup: on })
    expect(await screen.findByText('Model · work runs on standard: Standard (sonnet); subagents on haiku', {}, T)).toBeInTheDocument()
    expect(await screen.findByText(code("CLAUDE_CODE_SUBAGENT_MODEL=haiku orch session start --in background DEMO-0044 -- claude --model sonnet '/orch:work DEMO-0044'"), {}, T)).toBeInTheDocument()
  })
  it('an invalid model name shows the sentence and blocks Start in core\'s dialog', async () => {
    const { user } = renderApp('/ticket/DEMO-0044', { viewer: 'p_sev', setup: invalid })
    expect(await screen.findByText(SENTENCE, {}, T)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Start' }))
    const dialog = await screen.findByRole('dialog', { name: 'Start Claude Code on DEMO-0044' }, T)
    expect(within(dialog).getByRole('alert').textContent).toContain(SENTENCE)
    expect(within(dialog).getByRole('alert').textContent).toContain('Start is blocked by models')
    expect(within(dialog).getByRole('button', { name: 'Start agent' })).toBeDisabled()
    expect(mockStore.wsEventsOf(wsOf(mockStore)).some((e) => e.type === 'agent.started')).toBe(false)
  })
  it('the settings page warns about a setting that is not a model name', async () => {
    renderApp('/settings/addon/models', { viewer: 'p_sev', setup: invalid })
    expect(await screen.findByText('Standard is not a model name', {}, T)).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Standard model' })).toHaveValue('son net')
  })
})

afterEach(() => vi.restoreAllMocks())

describe('core\'s dialog renders the model fact itself', () => {
  it('shows the validated model and tier as fact, and the addon\'s line only under From addon', async () => {
    const { user } = renderApp('/ticket/DEMO-0044', { viewer: 'p_sev', setup: on })
    await user.click(await screen.findByRole('button', { name: 'Start' }, T))
    let dialog = await screen.findByRole('dialog', { name: 'Start Claude Code on DEMO-0044' }, T)
    expect(within(dialog).getByLabelText('What orch will start').textContent).toContain('sonnet (standard tier); subagents on haiku')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    vi.spyOn(getAddon('models')!, 'launch').mockReturnValue({ model: 'sonnet', tier: 'root', subagentModel: 'haiku', line: 'Approved by owner' })
    await user.click(screen.getByRole('button', { name: 'Start' }))
    dialog = await screen.findByRole('dialog', { name: 'Start Claude Code on DEMO-0044' }, T)
    const facts = within(dialog).getByLabelText('What orch will start')
    await waitFor(() => expect(facts.textContent).toContain('sonnet; subagents on haiku'), T)
    expect(facts.textContent).not.toContain('Approved by owner')
    expect(facts.textContent).not.toContain('root')
    const fromAddon = within(dialog).getByRole('region', { name: 'From addon models' })
    expect(fromAddon.textContent).toContain('Approved by owner')
  })
})

describe('escalation on Today', () => {
  it('core renders "T2 failed its check in two sessions" and Next start on Strong routes DEMO-0045 to opus', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev', setup: on })
    const q = (await screen.findAllByText('T2 failed its check in two sessions: start the next session on Strong?', {}, T))[0]
    const card = q.closest('[data-testid^="card-addon:"]') as HTMLElement
    expect(screen.getByRole('region', { name: /^From addons · \d+ · you sign every answer in orch/ })).toBeInTheDocument()
    await user.click(within(card).getByRole('button', { name: 'Decide' }))
    await user.click(within(card).getByRole('button', { name: 'Next start on Strong' }))
    await user.click(await screen.findByRole('button', { name: 'Sign with Touch ID' }, T))
    await waitFor(() => expect((mockStore.addonStateView(wsOf(mockStore), 'start-agent', 'DEMO-0045')!.previews as Record<string, { model?: string }>)['DEMO-0045'].model).toBe('Model · work runs on strong: Strong (opus); subagents on haiku'), T)
  })
})
