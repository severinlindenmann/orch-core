import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { vi } from 'vitest'
import { api } from '@/api/client'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 8000 }

describe('terminals page', () => {
  it('lists the sessions, states the rules, and opens a session into an xterm with a text input', async () => {
    const { user } = renderApp('/addon/terminals/sessions', { viewer: 'p_sev' })
    expect(await screen.findByText('Terminals are never granted to agents.', {}, T)).toBeInTheDocument()
    const stopped = (await screen.findByText('Scratch', {}, T)).closest('li')!
    expect(within(stopped).getByText('stopped')).toBeInTheDocument()
    const shell = (await screen.findByText('Severin · DEMO-0043 worktree', {}, T)).closest('li')!
    await user.click(within(shell).getByRole('button', { name: 'Open' }))
    const box = await screen.findByRole('textbox', {}, T)
    expect(box).toBeInTheDocument()
    expect(box).not.toHaveAttribute('aria-readonly', 'true')
    const term = document.querySelector('[data-terminal-session="shell1"]')!
    expect(term).not.toHaveAttribute('aria-readonly')
    await user.type(box, 'pwd{enter}')
    await waitFor(() => expect(term.textContent).toContain('/Users/severin/energy'), T)
  })
  it('the agent mirror is view-only: aria-readonly on the container and no typing', async () => {
    const { user } = renderApp('/addon/terminals/sessions', { viewer: 'p_sev' })
    const mirror = (await screen.findByText('agent: claude-code (read only, no typing)', {}, T)).closest('li')!
    await user.click(within(mirror).getByRole('button', { name: 'Open' }))
    await waitFor(() => expect(document.querySelector('[data-terminal-session="agent1"]')).not.toBeNull(), T)
    const term = document.querySelector('[data-terminal-session="agent1"]')!
    expect(term).toHaveAttribute('aria-readonly', 'true')
    await waitFor(() => expect(term.querySelector('textarea')).toHaveAttribute('aria-readonly', 'true'), T)
    await waitFor(() => expect(term.textContent).toContain('err human_only approve · retry:false · next: orch ask or orch wait'), T)
    await user.type(term.querySelector('textarea')!, 'ls{enter}')
    await new Promise((r) => setTimeout(r, 150)) // give a (wrongly) accepted keystroke time to run
    expect(term.textContent).not.toContain('dbt_project.yml')
    expect(term.textContent).not.toMatch(/\$ ls/)
  })
  it('a viewer opens the agent mirror view-only and has no New terminal', async () => {
    const { user } = renderApp('/addon/terminals/sessions', { viewer: 'p_tom' })
    const mirror = (await screen.findByText('agent: claude-code (read only, no typing)', {}, T)).closest('li')!
    const open = within(mirror).getByRole('button', { name: 'Open' })
    await waitFor(() => expect(open).toBeEnabled(), T)
    await user.click(open)
    await waitFor(() => expect(document.querySelector('[data-terminal-session="agent1"]')).toHaveAttribute('aria-readonly', 'true'), T)
    expect(screen.getByRole('button', { name: 'New terminal' })).toBeDisabled()
  })
  it('New terminal opens a fresh interactive shell', async () => {
    const { user } = renderApp('/addon/terminals/sessions', { viewer: 'p_mara' })
    const btn = await screen.findByRole('button', { name: 'New terminal' }, T)
    await waitFor(() => expect(btn).toBeEnabled(), T)
    await user.click(btn)
    await waitFor(() => expect(document.querySelector('[data-terminal-session]')).not.toBeNull(), T)
    expect(document.querySelector('[data-terminal-session]')).not.toHaveAttribute('aria-readonly')
  })
  it('a terminal node naming an unknown session is not shown', async () => {
    renderApp('/addon/terminals/sessions', {
      viewer: 'p_sev',
      setup: (s) => {
        const pkg = s.addons.find((a) => a.name === 'terminals')!
        const nav = pkg.contributions.find((c) => c.id === 'sessions')!
        ;(nav.node as { children: unknown[] }).children.push({ type: 'terminal', session: 'ghost' })
      },
    })
    await screen.findByText('Severin · DEMO-0043 worktree', {}, T)
    await waitFor(() => expect(screen.getByText('This addon panel could not be shown.')).toBeInTheDocument(), T)
  })
})

describe('terminals hostile text and exit', () => {
  it('a ticket title with OSC 8 / OSC 52 / CSI is printed as inert text: no link, no escape reaches the screen', async () => {
    const { user } = renderApp('/addon/terminals/sessions', {
      viewer: 'p_sev',
      setup: (s) => {
        const def = (s as unknown as { defs: Map<string, { title: string }> }).defs.get('DEMO-0043')!
        def.title = 'Seeds \x1b]8;;https://evil.example\x07click\x1b]8;;\x07 \x1b]52;c;ZXZpbA==\x07\x1b[2K\x1b[1A\x9b31m end'
      },
    })
    const box = await screen.findByRole('textbox', {}, T)
    await user.type(box, 'orch status{enter}')
    const term = document.querySelector('[data-terminal-session="shell1"]')!
    const rows = () => term.querySelector('.xterm-rows')?.textContent ?? ''
    await waitFor(() => expect(rows()).toContain('cursor'), T)
    expect(rows()).toContain('Seeds')
    expect(rows()).toContain('end')
    expect(term.querySelector('a[href]')).toBeNull()
    expect(rows()).not.toMatch(/\u001b|[\u0080-\u009f]/)
  })
  it('exit in your own shell closes the session once, however many keys follow', async () => {
    const spy = vi.spyOn(api, 'runAddonAction')
    const { user } = renderApp('/addon/terminals/sessions', { viewer: 'p_sev' })
    const box = await screen.findByRole('textbox', {}, T)
    await user.type(box, 'exit{enter}')
    await user.type(box, 'ab')
    await user.keyboard('{enter}')
    await new Promise((r) => setTimeout(r, 200))
    expect(spy.mock.calls.filter((c) => c[2] === 'close')).toHaveLength(1)
    const term = document.querySelector('[data-terminal-session="shell1"]')
    if (term) expect((term.querySelector('.xterm-rows')?.textContent ?? '').match(/process completed/g)?.length ?? 0).toBeLessThanOrEqual(1)
    spy.mockRestore()
    await waitFor(() => {
      const li = screen.getByText('Severin · DEMO-0043 worktree').closest('li')!
      expect(within(li).getByText('stopped')).toBeInTheDocument()
    }, T)
  })
  it('shows an empty state, not an error, when there are no visible sessions', async () => {
    renderApp('/addon/terminals/sessions', { viewer: 'p_sev', setup: (s) => void (s.addonState(s.workspaces.find((w) => w.prefix === 'DEMO')!.id, 'terminals').sessions = []) })
    expect(await screen.findByText('No terminal is open. Start one with New terminal.', {}, T)).toBeInTheDocument()
    expect(screen.queryByText('This addon panel could not be shown.')).not.toBeInTheDocument()
  })
  it('sizes to the container with the fit addon: no horizontal scroll wrapper', async () => {
    renderApp('/addon/terminals/sessions', { viewer: 'p_sev' })
    await screen.findByRole('textbox', {}, T)
    const term = document.querySelector('[data-terminal-session="shell1"]') as HTMLElement
    expect(term.className).not.toMatch(/overflow-x-auto/)
    expect(term.dataset.terminalRows).toBe('24') // page sizing
    expect(parseInt(term.style.height)).toBeGreaterThan(300)
  })
})

describe('terminals ticket panel', () => {
  it('Open terminal in this ticket\'s worktree shows the session inline in the panel', async () => {
    const { user } = renderApp('/ticket/DEMO-0041', { viewer: 'p_sev' })
    const panel = await screen.findByRole('complementary', { name: 'Ticket details' }, T)
    const frame = await waitFor(() => {
      const f = panel.querySelector('[data-addon="terminals"]')
      expect(f).not.toBeNull()
      return f as HTMLElement
    }, T)
    await user.click(within(frame).getByRole('button', { name: "Open terminal in this ticket's worktree" }))
    await waitFor(() => expect(panel.querySelector('[data-terminal-session]')).not.toBeNull(), T)
    const rail = panel.querySelector('[data-terminal-session]') as HTMLElement
    expect(rail.dataset.terminalRows).toBe('12') // rail sizing
    expect(parseInt(rail.style.height)).toBeLessThan(250)
  })
})
