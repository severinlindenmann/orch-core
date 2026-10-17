import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderApp } from '@/test/renderApp'
import { buildEdit } from './EditTicket'

vi.mock('@/api/features', async (orig) => ({ ...(await orig<typeof import('@/api/features')>()), handoffMode: () => true }))
// The signing dialog is its own feature (HandoffDialog tests); here it only shows the typed action it was given.
vi.mock('@/components/sign/HandoffDialog', () => ({
  HandoffDialog: (p: { title: string; action: unknown; onSettled?: (phase: string) => void; onClose: () => void }) => (
    <div role="dialog" aria-label={`sign ${p.title}`}>
      <pre data-testid="sent-action">{JSON.stringify(p.action)}</pre>
      <button onClick={() => p.onSettled?.('done')}>signed</button>
      <button onClick={p.onClose}>cancelled</button>
    </div>
  ),
}))

const T = { timeout: 5000 }
afterEach(() => vi.restoreAllMocks())

const ticket = {
  key: 'DEMO-0001',
  type: 'feature' as const,
  body: { summary: 'Old summary', requirements: 'Must work' },
  acceptance_state: [{ id: 'AC1', text: 'It opens', state: 'unproven' as const, evidence: [] }],
  people: { owner: 'p_a', assignees: ['p_b'], reviewers: [], watchers: [] },
  head: { seq: 7, hash: '' },
}
const none = { sections: {}, criteria: {}, added: [], people: { owner: 'p_a', assignees: ['p_b'], reviewers: [] } }

describe('buildEdit: what the person changed, as one typed action', () => {
  it('says nothing changed when nothing did', () => {
    expect(buildEdit(ticket, none, true)).toEqual({ nothing: true })
    expect(buildEdit(ticket, { ...none, sections: { summary: ' Old summary ' } }, true)).toEqual({ nothing: true })
  })
  it('carries only the changed sections, criteria and people, with the head it showed', () => {
    const made = buildEdit(
      ticket,
      {
        sections: { summary: 'New summary', requirements: 'Must work' },
        criteria: { AC1: 'It opens fast' },
        added: ['It saves', ' '],
        people: { owner: 'p_c', assignees: ['p_d'], reviewers: ['p_a'] },
      },
      true,
    )
    expect(made).toEqual({
      steps: 6,
      action: {
        action: 'edit_sequence',
        key: 'DEMO-0001',
        head_seq: 7,
        sections: { summary: 'New summary' },
        acceptance: { edit: { AC1: 'It opens fast' }, add: ['It saves'] },
        people: { owner: { add: ['p_c'], remove: [] }, assignees: { add: ['p_d'], remove: ['p_b'] }, reviewers: { add: ['p_a'], remove: [] } },
      },
    })
  })
  it('leaves the people out when the viewer may not change them', () => {
    expect(buildEdit(ticket, { ...none, people: { owner: 'p_c', assignees: [], reviewers: [] } }, false)).toEqual({ nothing: true })
  })
  it('keeps a spike\'s findings under the host\'s name', () => {
    const spike = { ...ticket, type: 'spike' as const, body: { verification: 'Found it' } }
    expect(buildEdit(spike, { ...none, sections: { verification: 'Found more' } }, true)).toMatchObject({ action: { sections: { findings: 'Found more' } } })
  })
  it('refuses to empty a section or a criterion, and too many changes', () => {
    expect(buildEdit(ticket, { ...none, sections: { summary: '  ' } }, true)).toHaveProperty('problem')
    expect(buildEdit(ticket, { ...none, criteria: { AC1: ' ' } }, true)).toHaveProperty('problem')
    expect(buildEdit(ticket, { ...none, added: Array.from({ length: 9 }, (_, i) => `c${i}`) }, true)).toHaveProperty('problem')
  })
})

describe('the ticket page edit', () => {
  it('edits text, a criterion and the people, then asks for ONE signature', async () => {
    const { user } = renderApp('/ticket/DEMO-0043')
    await screen.findByRole('heading', { level: 1, name: /Load tariff tables/ }, T)
    await user.click(await screen.findByRole('button', { name: 'Edit text, checks and people' }, T))
    const dialog = await screen.findByRole('dialog', { name: 'Edit DEMO-0043' }, T)
    expect(within(dialog).getByRole('button', { name: 'Review and sign' })).toBeInTheDocument()
    await user.type(within(dialog).getByLabelText('New acceptance criterion'), 'A header row is exported')
    await user.click(within(dialog).getByRole('button', { name: 'Add' }))
    await user.click(within(dialog).getByRole('button', { name: /^Review and sign \(1 change\)$/ }))
    const sent = JSON.parse((await screen.findByTestId('sent-action')).textContent ?? '{}')
    expect(sent).toMatchObject({ action: 'edit_sequence', key: 'DEMO-0043', acceptance: { edit: {}, add: ['A header row is exported'] } })
    expect(typeof sent.head_seq).toBe('number')
    await user.click(screen.getByRole('button', { name: 'signed' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Edit DEMO-0043' })).toBeNull(), T)
  })
  it('says so when nothing changed', async () => {
    const { user } = renderApp('/ticket/DEMO-0043')
    await user.click(await screen.findByRole('button', { name: 'Edit text, checks and people' }, T))
    const dialog = await screen.findByRole('dialog', { name: 'Edit DEMO-0043' }, T)
    await user.click(within(dialog).getByRole('button', { name: 'Review and sign' }))
    expect(await within(dialog).findByText('Nothing has changed yet.')).toBeInTheDocument()
    expect(screen.queryByTestId('sent-action')).toBeNull()
  })
})
