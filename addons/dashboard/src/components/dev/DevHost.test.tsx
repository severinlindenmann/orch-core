// Core D1 (#582) in the dashboard: the DEV SANDBOX badge, the alarms list, host.dev_refused reasons and the dev_host signing text.
import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { clickProblem } from '@/api/click'
import { hostInfoOf } from '@/api/hostInfo'
import { DEV_HOST_SENTENCE, DEV_REFUSED_GENERIC, DEV_REFUSED_SENTENCE, UNAVAILABLE_SENTENCE } from '@/api/refusals'
import { ApiError } from '@/api/types'
import { resetLayoutPreview } from '@/dev/layoutPreview'
import { renderApp } from '@/test/renderApp'
import { HostAlarms, IsolationProblems } from './DevHost'

const T = { timeout: 4000 }
afterEach(() => {
  cleanup()
  resetLayoutPreview()
})
const err = (code: string, detail?: Record<string, unknown>) => new ApiError(409, { code, message: `${code} from the host`, retryable: false, ...(detail ? { detail } : {}) } as never)

describe('hostInfoOf', () => {
  it('reads nothing from an answer without dev, or with dev false', () => {
    for (const raw of [undefined, null, {}, { dev: false }, { dev: { sim_convenience: false } }, 'dev', []]) expect(hostInfoOf(raw).dev).toBeNull()
  })
  it('reads dev as true or as the host sends it, and checks the sandbox id', () => {
    expect(hostInfoOf({ dev: true }).dev).toEqual({ sandbox: null })
    expect(hostInfoOf({ dev: { sim_convenience: true, sandbox: '0123abcd' } }).dev).toEqual({ sandbox: '0123abcd' })
    expect(hostInfoOf({ dev: { sim_convenience: true, sandbox: '<b>x</b>' } }).dev).toEqual({ sandbox: null })
  })
  it('validates alarms: a bad code is dropped, a bad workspace id is dropped', () => {
    const got = hostInfoOf({ alarms: [{ code: 'sandbox_workspace_registered', workspace_id: 'f'.repeat(32) }, { code: 'sandbox_workspace_registered', workspace_id: '../x' }, { code: 'A b' }, 3, { code: 'dev_convenience_refused', count: 7, text: 'x' }] })
    expect(got.alarms).toEqual([
      { code: 'sandbox_workspace_registered', workspace_id: 'f'.repeat(32) },
      { code: 'sandbox_workspace_registered', workspace_id: null },
      { code: 'dev_convenience_refused', workspace_id: null }, // unknown fields (count, text) are dropped
    ])
  })
})

describe('the DEV SANDBOX badge', () => {
  it('is drawn with an accessible label, with no way to dismiss it', async () => {
    renderApp('/__layout/dev-badge')
    const badge = await screen.findByTestId('dev-badge', {}, T)
    expect(badge).toHaveTextContent('DEV SANDBOX')
    expect(badge).toHaveTextContent('0123abcd')
    expect(badge.getAttribute('aria-label')).toMatch(/Dev sandbox host/)
    expect(within(badge).queryByRole('button')).toBeNull()
    expect(badge.closest('header')).not.toBeNull()
  })
  it('is drawn with a long workspace name too', async () => {
    renderApp('/__layout/dev-badge-long')
    expect(await screen.findByTestId('dev-badge', {}, T)).toBeInTheDocument()
  })
  it('is absent when the host says nothing about dev (normal host)', async () => {
    renderApp('/__layout/quick-normal')
    await screen.findByTestId('topbar-title', {}, T)
    await new Promise((r) => setTimeout(r, 150))
    expect(screen.queryByTestId('dev-badge')).toBeNull()
  })
})

describe('a failed host status read', () => {
  it('shows a neutral "Host status unavailable" indicator instead of looking like a normal host', async () => {
    renderApp('/__layout/dev-status-failed')
    const note = await screen.findByTestId('dev-status-unavailable', {}, T)
    expect(note).toHaveTextContent('Host status unavailable')
    expect(screen.queryByTestId('dev-badge')).toBeNull()
  })
})

describe('the alarms', () => {
  it('shows each known code as a sentence, an unknown one as "unknown alarm" plus the code only', async () => {
    renderApp('/__layout/dev-alarms')
    const list = await screen.findByTestId('host-alarms', {}, T)
    const rows = within(list).getAllByTestId('host-alarm').map((r) => r.textContent ?? '')
    expect(rows[0]).toMatch(/skipped a throwaway dev sandbox workspace/)
    expect(rows[0]).toContain('f'.repeat(32))
    expect(rows[1]).not.toContain('not-a-workspace-id') // an invalid workspace id is not shown
    expect(rows[2]).toMatch(/answer was refused\.$/)
    expect(rows[3]).toBe('unknown alarm some_future_alarm')
    expect(list.textContent).not.toContain('<b>')
    expect(rows).toHaveLength(4) // the code with invalid characters is dropped
  })
  it('draws nothing while there are none', () => {
    const { container } = render(<HostAlarms alarms={[]} />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('host.dev_refused', () => {
  it.each(Object.keys(DEV_REFUSED_SENTENCE))('reason %s is said in plain words', (reason) => {
    const p = clickProblem(err('host.dev_refused', { reason }))
    expect(p.body).toBe(DEV_REFUSED_SENTENCE[reason])
    expect(p.body).not.toContain(reason)
  })
  it('an unknown reason and a missing one get the generic text, never the reason', () => {
    for (const detail of [{ reason: 'brand_new_reason' }, undefined]) {
      const p = clickProblem(err('host.dev_refused', detail))
      expect(p.body).toBe(DEV_REFUSED_GENERIC)
      expect(p.body).not.toContain('brand_new')
    }
  })
  it('the gallery draws every sample', async () => {
    renderApp('/__layout/dev-refused')
    const gallery = await screen.findByTestId('dev-refused-gallery', {}, T)
    expect(within(gallery).getAllByTestId('click-problem').length).toBe(9)
    expect(gallery.textContent).toContain(DEV_REFUSED_SENTENCE.sandbox_workspace)
    expect(gallery.textContent).toContain(DEV_REFUSED_GENERIC)
    expect(gallery.textContent).not.toContain('brand_new_reason')
  })
})

describe('the dev_host signing refusal', () => {
  it('sign.convenience_unavailable · dev_host explains it and does not offer the "turn it off" advice', () => {
    const p = clickProblem(err('sign.convenience_unavailable', { reason: 'dev_host' }))
    expect(p.body).toBe(UNAVAILABLE_SENTENCE.dev_host)
    expect(p.body).toContain('Signing from the dashboard is off on a dev sandbox host')
    expect(p.body).not.toContain('orch host convenience off')
  })
  it('the signing card says so and offers no switch', async () => {
    renderApp('/__layout/signing-dev-host')
    const note = await screen.findByTestId('signing-dev-host', {}, T)
    expect(note).toHaveTextContent(DEV_HOST_SENTENCE)
    expect(screen.queryByRole('button', { name: 'Turn on…' })).toBeNull()
    expect(await screen.findByTestId('dev-badge')).toBeInTheDocument()
  })
})

describe('the isolation problems (L13)', () => {
  it('draws nothing when there are none', () => {
    render(<IsolationProblems problems={[]} />)
    expect(screen.queryByTestId('isolation-problems')).toBeNull()
  })
  it('draws each problem with its fix as plain text, never as markup', () => {
    render(<IsolationProblems problems={[{ what: '/home/a/.netrc holds a password', fix: 'chmod 600 -- /home/a/.netrc <b>x</b>' }]} />)
    const row = screen.getByTestId('isolation-problem')
    expect(row.textContent).toContain('/home/a/.netrc holds a password')
    expect(row.textContent).toContain('<b>x</b>')
    expect(row.querySelector('b')).toBeNull()
  })
})
