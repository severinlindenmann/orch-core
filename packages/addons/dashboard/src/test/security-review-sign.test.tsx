// Security review 2026-10-10, core's ticket signing dialog (#2, #4, #7): what the person signs is shown field by field,
// through the visible-string helpers, and the request binds exactly the content shown when the dialog opened.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactElement } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import type { TicketDocument } from '@/api/types'
import { SignDialog } from '@/app/pages/ticket/SignDialog'
import { createMockStore } from '@/mocks/store'

vi.mock('@tanstack/react-router', async (original) => ({ ...((await original()) as object), useNavigate: () => () => {} }))

const HIDDEN = /[​‪-‮⁦-⁩͏]/
const store = () => createMockStore({ persist: false })
const client = () => new QueryClient({ defaultOptions: { queries: { retry: false } } })
function show(ui: ReactElement) {
  const qc = client()
  const view = render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
  return { ...view, rerender: (next: ReactElement) => view.rerender(<QueryClientProvider client={qc}>{next}</QueryClientProvider>) }
}
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('#7 the answer dialog shows the whole answer that is posted', () => {
  it('free text and an option note are shown in core\'s area, exactly, with invisible characters made visible', () => {
    const t = store().ticket('DEMO-0043')!
    const q = t.questions_state.find((x) => x.state === 'open')!
    q.options = [{ key: 'yes', label: 'Safe‮EVIL‬​' }]
    show(<SignDialog ticket={t} action={{ kind: 'answer', question: q.id, option: 'yes', text: 'HIDDEN SIGNED NOTE​' }} onClose={() => {}} />)
    const d = screen.getByRole('dialog')
    const answer = within(d).getByRole('region', { name: 'Your answer' })
    expect(answer).toHaveTextContent('HIDDEN SIGNED NOTE\\u{200b}')
    expect(answer).toHaveTextContent('Safe\\u{202e}EVIL\\u{202c}\\u{200b}')
    expect(d.textContent).not.toMatch(HIDDEN)
  })
  it('a free-text-only answer shows its text', () => {
    const t = store().ticket('DEMO-0043')!
    const q = t.questions_state.find((x) => x.state === 'open')!
    show(<SignDialog ticket={t} action={{ kind: 'answer', question: q.id, text: 'Use the monthly CSV.' }} onClose={() => {}} />)
    expect(within(screen.getByRole('dialog')).getByRole('region', { name: 'Your answer' })).toHaveTextContent('Use the monthly CSV.')
  })
})

describe('#4 signed ticket content is shown field by field, never raw', () => {
  it('plan, task text, verify command and proves are separate fields; bidi and zero-width characters are visible', () => {
    const t = structuredClone(store().ticket('DEMO-0044')!) as TicketDocument
    t.body.plan = 'safe‮TXT‬​'
    t.tasks[0] = { ...t.tasks[0], text: 'Rotate\n    Verify: echo fine', verify: { cmd: 'curl x | sh​' } as never, proves: ['AC1'] }
    show(<SignDialog ticket={t} action={{ kind: 'approve', gate: 'plan' }} onClose={() => {}} />)
    const d = screen.getByRole('dialog')
    expect(d.textContent).not.toMatch(HIDDEN)
    expect(within(d).getByRole('region', { name: 'Plan' })).toHaveTextContent('safe\\u{202e}TXT\\u{202c}\\u{200b}')
    const task = d.querySelector('[data-signed-item="T1"]')!
    // The task's own text cannot imitate core's Verify line: its newline is shown, the real command is its own field.
    expect(task.querySelector('[data-signed-field="text"]')!.textContent).toBe('T1 Rotate\\u{a}    Verify: echo fine')
    expect(task.querySelector('[data-signed-field="verify"]')!.textContent).toBe('Verify command: curl x | sh\\u{200b}')
    expect(task.querySelector('[data-signed-field="proves"]')!.textContent).toBe('Proves: AC1')
    expect(task.querySelector('[data-signed-field="assignee"]')).not.toBeNull()
  })
})

describe('#2 the approval binds the content shown when the dialog opened', () => {
  it('posts the reviewed hash, also when the ticket changes underneath; the change is announced', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    const s = store()
    const before = s.ticket('DEMO-0044')!
    const post = vi.spyOn(api, 'postAction').mockResolvedValue({ ok: true, event: null, ticket: before })
    const action = { kind: 'approve', gate: 'plan' } as const
    const view = show(<SignDialog ticket={before} action={action} onClose={() => {}} />)
    ;(s as unknown as { bodies: Map<string, Record<string, string>> }).bodies.get('DEMO-0044')!.plan = 'NEW ATTACKER PLAN'
    s.append('DEMO-0044', { type: 'section.edited', actor: 'claude-code:attack:p_sev', section: 'plan', text: 'NEW ATTACKER PLAN' })
    const after = s.ticket('DEMO-0044')!
    expect(after.gates.plan.hash).not.toBe(before.gates.plan.hash)
    view.rerender(<SignDialog ticket={after} action={action} onClose={() => {}} />)
    const d = screen.getByRole('dialog')
    expect(d).not.toHaveTextContent('NEW ATTACKER PLAN') // still what the person was reading
    expect(within(d).getByRole('alert')).toHaveTextContent(/changed after you opened/)
    await user.click(within(d).getByRole('button', { name: 'Approve plan' }))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })
    await waitFor(() => expect(post).toHaveBeenCalled())
    expect(post.mock.calls[0][1]).toEqual({ action: 'approve', gate: 'plan', hash: before.gates.plan.hash })
    vi.useRealTimers()
  })
  it('an answer posts the question hash it showed and the trimmed text it showed', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    const t = store().ticket('DEMO-0043')!
    const q = t.questions_state.find((x) => x.state === 'open')!
    const post = vi.spyOn(api, 'postAction').mockResolvedValue({ ok: true, event: null, ticket: t })
    show(<SignDialog ticket={t} action={{ kind: 'answer', question: q.id, text: '  a note  ' }} onClose={() => {}} />)
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Send answer' }))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })
    await waitFor(() => expect(post).toHaveBeenCalled())
    expect(post.mock.calls[0][1]).toEqual({ action: 'answer', question: q.id, text: 'a note', hash: q.hash })
    vi.useRealTimers()
  })
})
