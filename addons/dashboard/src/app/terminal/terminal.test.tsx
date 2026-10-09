import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { beforeEach, afterEach, vi } from 'vitest'
import { api } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { openTicketPanel } from '@/test/ticketPanels'

const T = { timeout: 8000 }
beforeEach(() => {
  vi.stubGlobal('innerWidth', 1440)
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(900)
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(500)
})
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('terminals page', () => {
  it('lists the sessions, states the rules, and opens a session into an xterm with a text input', async () => {
    const { user } = renderApp('/addon/terminals/sessions', { viewer: 'p_sev' })
    expect(await screen.findByText('Your shells are private to you. Agent sessions are view-only mirrors; agents never get a terminal.', {}, T)).toBeInTheDocument()
    const sessions = await screen.findByRole('navigation', { name: 'Terminal sessions' }, T)
    await user.click(within(sessions).getByRole('button', { name: /DEMO-0043 · Your shell/ }))
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
    await user.click(await screen.findByRole('button', { name: /DEMO-0043 · Claude Code/ }, T))
    expect(await screen.findByText('Agent output · view only')).toBeInTheDocument()
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
    await user.click(await screen.findByRole('button', { name: /DEMO-0043 · Claude Code/ }, T))
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
    await screen.findByRole('button', { name: /DEMO-0043 · Your shell/ }, T)
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
    expect(await screen.findByText('Ended', {}, T)).toBeInTheDocument()
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
    // fills the viewport below its own top edge, never under 360 px
    await waitFor(() => expect(parseInt(term.style.height)).toBeGreaterThanOrEqual(360), T)
    expect(term.style.minHeight).toBe('360px')
  })
})

describe('terminals ticket panel', () => {
  it('Open terminal in this ticket\'s worktree shows the session inline in the panel', async () => {
    const { user } = renderApp('/ticket/DEMO-0041', { viewer: 'p_sev' })
    const panel = await screen.findByRole('complementary', { name: 'Ticket details' }, T)
    await openTicketPanel(user, 'Terminal')
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

describe('terminal keyboard exits and lifecycle', () => {
  it('releases mirror Tab, Shift+Tab and Escape and explains refused typing', async () => {
    const { user } = renderApp('/addon/terminals/sessions', { viewer: 'p_sev' })
    await user.click(await screen.findByRole('button', { name: /DEMO-0043 · Claude Code/ }, T))
    const group = await screen.findByRole('group', { name: 'Terminal: DEMO-0043 · Claude Code' })
    const input = within(group).getByRole('textbox')
    for (const key of ['Tab', 'ShiftTab', 'Escape']) {
      input.focus()
      if (key === 'Escape') await user.keyboard('{Escape}')
      else await user.tab({ shift: key === 'ShiftTab' })
      expect(group).not.toContainElement(document.activeElement as HTMLElement)
    }
    await user.type(input, 'hello')
    expect(screen.getByRole('status')).toHaveTextContent('Agent output is view only. Open your own shell to type.')
  })
  it('lets a shell use Tab and single Escape, then releases double Escape', async () => {
    const { user } = renderApp('/addon/terminals/sessions', { viewer: 'p_sev' })
    const input = await screen.findByRole('textbox', {}, T)
    input.focus()
    await user.tab()
    expect(input).toHaveFocus()
    await user.keyboard('{Escape}')
    expect(input).toHaveFocus()
    await user.keyboard('{Escape}')
    expect(screen.getByRole('button', { name: 'Leave terminal' })).toHaveFocus()
  })
  it('mounts, unmounts and remounts without console errors', async () => {
    const errors = vi.spyOn(console, 'error')
    const first = renderApp('/addon/terminals/sessions', { viewer: 'p_sev' })
    await screen.findByRole('textbox', {}, T)
    first.unmount()
    renderApp('/addon/terminals/sessions', { viewer: 'p_sev' })
    await screen.findByRole('textbox', {}, T)
    expect(errors).not.toHaveBeenCalled()
  })
})

describe('terminal workspace layout', () => {
  it('below 1280 the session list becomes a picker in the header', async () => {
    vi.stubGlobal('innerWidth', 1024)
    const { user } = renderApp('/addon/terminals/sessions', { viewer: 'p_sev' })
    await screen.findByRole('group', { name: /Terminal:/ }, T)
    expect(screen.queryByRole('navigation', { name: 'Terminal sessions' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Choose terminal session' }))
    const list = await screen.findByRole('navigation', { name: 'Terminal sessions' })
    await user.click(within(list).getByRole('button', { name: /DEMO-0043 · Claude Code/ }))
    expect(await screen.findByText('Agent output · view only')).toBeInTheDocument()
    expect(screen.queryByRole('navigation', { name: 'Terminal sessions' })).not.toBeInTheDocument()
  })
  it('keeps at most two live terminals; the evicted one shows its last lines and re-attaches on click', async () => {
    const apps = [renderApp('/addon/terminals/sessions', { viewer: 'p_sev' })]
    await screen.findByRole('group', { name: /Terminal:/ }, T)
    apps.push(renderApp('/addon/terminals/sessions', { viewer: 'p_sev' }))
    await waitFor(() => expect(document.querySelectorAll('[data-terminal-session]')).toHaveLength(2), T)
    apps.push(renderApp('/addon/terminals/sessions', { viewer: 'p_sev' }))
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Attach terminal' })).toHaveLength(1), T)
    expect(document.querySelectorAll('[data-terminal-session]')).toHaveLength(2)
    expect(screen.getByLabelText(/Last 20 lines/)).toBeInTheDocument()
    await apps[0].user.click(screen.getByRole('button', { name: 'Attach terminal' }))
    await waitFor(() => expect(document.querySelectorAll('[data-terminal-session]')).toHaveLength(2), T)
    expect(screen.getAllByRole('button', { name: 'Attach terminal' })).toHaveLength(1)
  }, 30000)
  it('leave terminal moves focus out of the terminal; Find is Cmd+F only and Ctrl+F reaches the shell', async () => {
    const { user } = renderApp('/addon/terminals/sessions', { viewer: 'p_sev' })
    const input = await screen.findByRole('textbox', {}, T)
    input.focus()
    await user.keyboard('{Control>}f{/Control}')
    expect(screen.queryByRole('textbox', { name: 'Find in terminal' })).not.toBeInTheDocument()
    await user.keyboard('{Meta>}f{/Meta}')
    expect(await screen.findByRole('textbox', { name: 'Find in terminal' })).toBeInTheDocument()
    const group = screen.getByRole('group', { name: /Terminal:/ })
    await user.click(screen.getByRole('button', { name: 'Leave terminal' }))
    expect(group).not.toContainElement(document.activeElement as HTMLElement)
    expect(document.activeElement).not.toBe(screen.getByRole('button', { name: 'Leave terminal' }))
  })
  it('opens Ended when the selected session is in it', async () => {
    const { user } = renderApp('/addon/terminals/sessions', { viewer: 'p_sev' })
    const list = await screen.findByRole('navigation', { name: 'Terminal sessions' }, T)
    await user.click(within(list).getByText(/Ended \(2\)/))
    await user.click(within(list).getByRole('button', { name: /Scratch shell/ }))
    await waitFor(() => expect(within(screen.getByRole('navigation', { name: 'Terminal sessions' })).getByRole('button', { name: /Scratch shell/ })).toHaveAttribute('aria-current', 'true'), T)
    expect(document.querySelector('details')).toHaveAttribute('open')
  })
  it('stops following when the person scrolls up and offers Jump to latest', async () => {
    const { user } = renderApp('/addon/terminals/sessions', { viewer: 'p_sev', setup: (s) => s.reset('busy') })
    await user.click(await screen.findByRole('button', { name: /DEMO-0117 · Claude Code/ }, T))
    const group = await screen.findByRole('group', { name: 'Terminal: DEMO-0117 · Claude Code' }, T)
    await waitFor(() => expect(group.querySelector('.xterm-viewport')).not.toBeNull(), T)
    expect(screen.queryByRole('button', { name: /Jump to latest/ })).not.toBeInTheDocument()
    const input = within(group).getByRole('textbox')
    await waitFor(() => {
      input.focus()
      fireEvent.keyDown(input, { key: 'PageUp', code: 'PageUp', keyCode: 33, shiftKey: true })
      expect(screen.getByRole('button', { name: /Jump to latest/ })).toBeInTheDocument()
    }, T)
    await user.click(screen.getByRole('button', { name: /Jump to latest/ }))
    expect(screen.queryByRole('button', { name: /Jump to latest/ })).not.toBeInTheDocument()
  })
})
