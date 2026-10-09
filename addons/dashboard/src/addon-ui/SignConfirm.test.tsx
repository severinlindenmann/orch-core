import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { SignConfirm } from './SignConfirm'

const SPOOF = 'Approve DEMO-0042 for release'
const draw = () =>
  render(
    <QueryClientProvider client={new QueryClient()}>
      <SignConfirm addon="schedules" addonTitle="Schedules" action="arm" workspace={{ prefix: 'DEMO', name: 'Acme Energy' }} label={SPOOF} args={{ id: 'check-inbox' }} onSign={() => {}} onClose={() => {}} />
    </QueryClientProvider>,
  )

describe('SignConfirm', () => {
  it('writes the title and the covers itself; the addon\'s label only appears in the From-addon region', () => {
    draw()
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByRole('heading', { name: 'Sign: Arm (arm) · Schedules (schedules)' })).toBeInTheDocument()
    expect(within(dialog).getByText(/In workspace Acme Energy \(DEMO\)/)).toBeInTheDocument()
    const covers = within(dialog).getByText('Covers').nextElementSibling!
    expect(covers.textContent).not.toContain(SPOOF)
    expect(dialog.querySelector('h2')!.textContent).not.toContain(SPOOF)
    const region = within(dialog).getByRole('region', { name: 'From addon schedules' })
    expect(region.textContent).toContain(SPOOF)
    expect(region.textContent).toContain('Id (id): check-inbox')
    expect(region.className).toMatch(/dashed/)
  })

  it('always shows every sent arg; the addon-written subject is extra, labelled context in the From-addon region', () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <SignConfirm addon="publish" addonTitle="Publish" action="revoke" workspace={{ prefix: 'DEMO', name: 'Acme Energy' }} args={{ share: 'B' }} subject="Revoke share A" onSign={() => {}} onClose={() => {}} />
      </QueryClientProvider>,
    )
    const dialog = screen.getByRole('dialog')
    const region = within(dialog).getByRole('region', { name: 'From addon publish' })
    expect(region.textContent).toContain('Share (share): B')
    expect(region.textContent).toMatch(/Addon says:\s*Revoke share A/)
    expect(within(dialog).getByText('Covers').nextElementSibling!.textContent).not.toContain('Revoke share A')
  })

  it('keeps the exact signed values: raw action ids, raw arg keys and values, and the package id beside any title', () => {
    const one = (action: string, title = 'Schedules', addon = 'schedules', args: Record<string, unknown> = { schedule_id: 'smoke-on-testing' }, subject?: string) => {
      const view = render(
        <QueryClientProvider client={new QueryClient()}>
          <SignConfirm addon={addon} addonTitle={title} action={action} workspace={{ prefix: 'DEMO', name: 'Acme Energy' }} args={args} subject={subject} onSign={() => {}} onClose={() => {}} />
        </QueryClientProvider>,
      )
      const dialog = screen.getByRole('dialog')
      const out = { heading: within(dialog).getByRole('heading').textContent!, covers: within(dialog).getByText('Covers').nextElementSibling!.textContent!, region: within(dialog).getByRole('region').textContent! }
      view.unmount()
      return out
    }
    // Two actions that read alike in words still show different exact ids.
    const a = one('arm_schedule')
    const b = one('arm-schedule')
    expect(a.heading).toBe('Sign: Arm schedule (arm_schedule) · Schedules (schedules)')
    expect(b.heading).toBe('Sign: Arm schedule (arm-schedule) · Schedules (schedules)')
    expect(a.covers).toContain('Runs "Arm schedule" (arm_schedule) of the addon Schedules (schedules)')
    expect(b.covers).toContain('(arm-schedule)')
    // An arg whose label is its words also shows its raw key, and the value verbatim.
    expect(a.region).toContain('Schedule id (schedule_id): smoke-on-testing')
    // The addon's own words stay labelled as the addon's.
    const c = one('run_now', 'Schedules', 'schedules', { id: 'smoke-on-testing' }, 'Smoke on testing')
    expect(c.region).toMatch(/Addon says:\s*Smoke on testing/)
    expect(c.region).toContain('Id (id): smoke-on-testing')
    // An addon that calls itself "orch core" still shows its package id in core's lines.
    const d = one('arm', 'orch core', 'evil-addon')
    expect(d.heading).toBe('Sign: Arm (arm) · orch core (evil-addon)')
    expect(d.covers).toContain('of the addon orch core (evil-addon)')
    expect(d.region).toContain('From the addon orch core (evil-addon)')
    // A title that is the id is not said twice.
    expect(one('arm', 'schedules', 'schedules').heading).toBe('Sign: Arm (arm) · schedules')
  })

  it('covers the render-context ticket (posted as `ticket`); args core cannot show block the sign button', () => {
    const view = render(
      <QueryClientProvider client={new QueryClient()}>
        <SignConfirm addon="schedules" addonTitle="Schedules" action="arm" workspace={{ prefix: 'DEMO', name: 'Acme Energy' }} args={{ id: 'a' }} ticket="DEMO-0041" onSign={() => {}} onClose={() => {}} />
      </QueryClientProvider>,
    )
    expect(within(screen.getByRole('dialog')).getByText('Covers').nextElementSibling).toHaveTextContent('About DEMO-0041')
    view.unmount()
    render(
      <QueryClientProvider client={new QueryClient()}>
        <SignConfirm addon="schedules" addonTitle="Schedules" action="arm" workspace={{ prefix: 'DEMO', name: 'Acme Energy' }} args={{ id: 'a', more: { x: 1 } }} onSign={() => {}} onClose={() => {}} />
      </QueryClientProvider>,
    )
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByRole('alert')).toHaveTextContent(/nothing was signed or sent/)
    expect(within(dialog).getByRole('button', { name: 'Sign and run' })).toBeDisabled()
  })

  it('names its verb, says how you confirm, starts on Cancel and has no repeated line', () => {
    draw()
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByRole('button', { name: 'Sign and run' })).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: /signs with Touch ID/i })).toBeNull()
    expect(dialog).toHaveTextContent('You confirm with Touch ID or your key.')
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toHaveFocus()
    expect(within(dialog).queryByText('Signed as you, with your own key')).toBeNull()
  })
})
