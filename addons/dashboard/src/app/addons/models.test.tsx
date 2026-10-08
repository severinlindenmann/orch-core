import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import type { MockStore } from '@/mocks/store'
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
    expect(screen.getByText(code("CLAUDE_CODE_SUBAGENT_MODEL=haiku orch session start --in background DEMO-0044 -- claude --model sonnet '/orch:work DEMO-0044'"))).toBeInTheDocument()
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

describe('escalation on Today', () => {
  it('core renders "T2 failed its check in two sessions" and Next start on Strong routes DEMO-0045 to opus', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev', setup: on })
    const q = await screen.findByText('T2 failed its check in two sessions: start the next session on Strong?', {}, T)
    const card = q.closest('[data-testid^="card-addon:"]') as HTMLElement
    expect(within(card).getByText(/confirmed and signed by orch/)).toBeInTheDocument()
    await user.click(within(card).getByRole('button', { name: 'Next start on Strong' }))
    await waitFor(() => expect((mockStore.addonStateView(wsOf(mockStore), 'start-agent')!.previews as Record<string, { model?: string }>)['DEMO-0045'].model).toBe('Model · work runs on strong: Strong (opus); subagents on haiku'), T)
  })
})
