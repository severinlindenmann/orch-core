import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
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
    expect(term.textContent).not.toMatch(/\$ ls$/m)
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
  })
})
