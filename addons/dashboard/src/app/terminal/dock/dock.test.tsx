import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 10000 }
beforeEach(() => {
  vi.stubGlobal('innerWidth', 1440)
  vi.stubGlobal('innerHeight', 900)
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(900)
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(300)
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

const bar = () => screen.findByRole('complementary', { name: 'Terminal dock' }, T)
const dock = () => screen.findByRole('region', { name: 'Terminal dock' }, T)
const openDock = async (user: ReturnType<typeof renderApp>['user']) => {
  await user.click(within(await bar()).getByRole('button', { name: /Open terminal dock/ }))
  return dock()
}
const prefs = (viewer = 'p_sev') => JSON.parse(localStorage.getItem(`orch.dock.${viewer}`) ?? 'null')

describe('terminal dock: open, collapse, resize, side', { timeout: 40000 }, () => {
  it('starts as a 32 px bar; Ctrl+` opens it, the collapse button and Ctrl+` collapse it', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev' })
    expect(await bar()).toHaveStyle({ height: '32px' })
    fireEvent.keyDown(window, { key: '`', code: 'Backquote', ctrlKey: true })
    const d = await dock()
    expect(d).toHaveAttribute('data-dock-side', 'bottom')
    await user.click(within(d).getByRole('button', { name: 'Collapse terminal dock' }))
    await bar()
    expect(screen.queryByRole('region', { name: 'Terminal dock' })).not.toBeInTheDocument()
    fireEvent.keyDown(window, { key: '`', code: 'Backquote', ctrlKey: true })
    await dock()
    fireEvent.keyDown(window, { key: '`', code: 'Backquote', ctrlKey: true })
    await bar()
    expect(prefs().open).toBe(false)
  })
  it('the page area shrinks beside the dock: the dock is a sibling of the page, not an overlay', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev' })
    const d = await openDock(user)
    const main = document.querySelector('main')!
    expect(d.contains(main)).toBe(false)
    expect(d.parentElement).toBe(main.parentElement!.parentElement)
    expect(d.className).not.toMatch(/\b(fixed|absolute)\b/)
  })
  it('resizes by keyboard within 160 px and 70% of the height, and remembers the size', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev' })
    const d = await openDock(user)
    const handle = within(d).getByRole('separator', { name: 'Resize terminal dock' })
    expect(handle).toHaveAttribute('aria-valuenow', '280')
    expect(handle).toHaveAttribute('aria-valuemax', '630')
    handle.focus()
    await user.keyboard('{ArrowUp}')
    expect(handle).toHaveAttribute('aria-valuenow', '296')
    await user.keyboard('{Shift>}{ArrowDown}{/Shift}')
    expect(handle).toHaveAttribute('aria-valuenow', '232')
    await user.keyboard('{Home}')
    expect(handle).toHaveAttribute('aria-valuenow', '160')
    await user.keyboard('{End}')
    expect(handle).toHaveAttribute('aria-valuenow', '630')
    expect(d).toHaveStyle({ height: '630px' })
    expect(prefs().bottom).toBe(630)
  })
  it('resizes by dragging the edge', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev' })
    const d = await openDock(user)
    const handle = within(d).getByRole('separator', { name: 'Resize terminal dock' })
    fireEvent.pointerDown(handle, { clientY: 600, pointerId: 1 })
    fireEvent.pointerMove(handle, { clientY: 500, pointerId: 1 })
    fireEvent.pointerUp(handle, { pointerId: 1 })
    expect(handle).toHaveAttribute('aria-valuenow', '380')
  })
  it('moves to the right side (320 px to 60% of the width) and the choice survives a reload', async () => {
    vi.spyOn(Storage.prototype, 'clear').mockImplementation(() => {}) // keep the prefs across the second render
    const first = renderApp('/', { viewer: 'p_sev', setup: () => localStorage.removeItem('orch.dock.p_sev') })
    const d = await openDock(first.user)
    await first.user.click(within(d).getByRole('button', { name: 'Move dock to the right' }))
    await waitFor(() => expect(d).toHaveAttribute('data-dock-side', 'right'))
    const handle = within(d).getByRole('separator', { name: 'Resize terminal dock' })
    expect(handle).toHaveAttribute('aria-orientation', 'vertical')
    expect(handle).toHaveAttribute('aria-valuemax', '864')
    handle.focus()
    await first.user.keyboard('{ArrowLeft}')
    expect(handle).toHaveAttribute('aria-valuenow', '456')
    expect(prefs()).toMatchObject({ side: 'right', open: true, right: 456 })
    first.unmount()
    renderApp('/', { viewer: 'p_sev' })
    const again = await dock()
    expect(again).toHaveAttribute('data-dock-side', 'right')
    expect(again).toHaveStyle({ width: '456px' })
    localStorage.removeItem('orch.dock.p_sev')
  })
  it('is not shown where the terminals addon may not use pty', async () => {
    renderApp('/', {
      viewer: 'p_sev',
      setup: (s) => {
        s.workspaces.find((w) => w.prefix === 'DEMO')!.addons.terminals.enabled = false
      },
    })
    await screen.findByRole('heading', { name: 'Today' }, T)
    await screen.findByRole('link', { name: /Usage/ }, T) // the addon list has loaded (it decides the gate)
    expect(screen.queryByRole('link', { name: /Terminals/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('complementary', { name: 'Terminal dock' })).not.toBeInTheDocument()
    fireEvent.keyDown(window, { key: '`', code: 'Backquote', ctrlKey: true })
    expect(screen.queryByRole('region', { name: 'Terminal dock' })).not.toBeInTheDocument()
  })
})

describe('terminal dock on a ticket page', { timeout: 40000 }, () => {
  it('lists this ticket\'s sessions only: running to join or watch, earlier ones with transcript and resume', async () => {
    const { user } = renderApp('/ticket/DEMO-0043', { viewer: 'p_sev' })
    const d = await openDock(user)
    const sessions = await within(d).findByRole('region', { name: 'Sessions for DEMO-0043' }, T)
    expect(within(sessions).getByRole('button', { name: 'Join DEMO-0043 · Your shell' })).toBeInTheDocument()
    expect(within(sessions).getByRole('button', { name: 'Watch DEMO-0043 · Claude Code (view only)' })).toBeInTheDocument()
    expect(within(sessions).getByRole('button', { name: 'Transcript of DEMO-0043 · Codex' })).toBeInTheDocument()
    expect(within(sessions).getByText(/Reviewed the DEMO-0043 plan/)).toBeInTheDocument()
    expect(within(sessions).queryByText(/Scratch shell/)).not.toBeInTheDocument() // no ticket: not this ticket's
    // tmux-style status line: session name, windows, ticket
    const windows = within(d).getByRole('tablist', { name: 'Session windows' })
    expect(within(windows).getAllByRole('tab').map((t) => t.textContent)).toEqual(['0:shell* DEMO-0043 · Your shell', '1:claude~ DEMO-0043 · Claude Code'])
    expect(d.querySelector('[data-tmux-status]')!.textContent).toMatch(/\[DEMO\].*"DEMO-0043".*11:30 09-Oct/)
  })
  it('a viewer sees the agent sessions to watch, not other people\'s shells, and cannot start or resume', async () => {
    const { user } = renderApp('/ticket/DEMO-0043', { viewer: 'p_tom' })
    const d = await openDock(user)
    const sessions = await within(d).findByRole('region', { name: 'Sessions for DEMO-0043' }, T)
    await within(sessions).findByRole('button', { name: /Watch DEMO-0043 · Claude Code/ }, T)
    expect(within(sessions).queryByText(/Your shell/)).not.toBeInTheDocument()
    expect(within(sessions).getByRole('button', { name: /^Start/ })).toBeDisabled()
    expect(within(sessions).getByRole('button', { name: 'Resume DEMO-0043 · Codex' })).toBeDisabled()
  })
  it('the workspace dock leaves out sessions of tickets the viewer cannot see', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev', setup: (s) => void vi.spyOn(s, 'isVisible').mockImplementation((key: string) => key !== 'DEMO-0043') }) // a spy: restored after the test
    const d = await openDock(user)
    await within(d).findByRole('button', { name: 'Sessions' }, T)
    await user.click(within(d).getByRole('button', { name: 'Sessions' }))
    const sessions = await within(d).findByRole('region', { name: 'Sessions in this workspace' }, T)
    expect(within(sessions).getByText('Scratch shell')).toBeInTheDocument()
    expect(within(sessions).queryByText(/DEMO-0043/)).not.toBeInTheDocument()
  })
  it('joining an agent session is view only; its Claude Code transcript renders through clean()', async () => {
    const { user } = renderApp('/ticket/DEMO-0043', {
      viewer: 'p_sev',
      setup: (s) => {
        const def = (s as unknown as { defs: Map<string, { title: string }> }).defs.get('DEMO-0043')!
        def.title = 'Seeds \x1b]8;;https://evil.example\x07click\x1b]8;;\x07 \x9b31m end'
      },
    })
    const d = await openDock(user)
    await user.click(await within(d).findByRole('button', { name: /Watch DEMO-0043 · Claude Code/ }, T))
    await waitFor(() => expect(d.querySelector('[data-terminal-session="agent1"]')).not.toBeNull(), T)
    const term = d.querySelector('[data-terminal-session="agent1"]')!
    expect(term).toHaveAttribute('aria-readonly', 'true')
    expect(within(d).getByText('Agent output · view only')).toBeInTheDocument()
    const rows = () => term.querySelector('.xterm-rows')?.textContent ?? ''
    // The visible rows (the screen follows the end): tool calls, the human-only refusal, the working line.
    await waitFor(() => expect(rows()).toContain('esc to interrupt'), T)
    expect(rows()).toContain('⏺ Bash(orch approve DEMO-0043 plan)')
    expect(rows()).toContain('err human_only approve')
    expect(rows()).toContain('Seeds ]8;;https://evil.example')
    expect(rows()).not.toMatch(/[\u001b\u0080-\u009f]/)
    expect(term.querySelector('a[href]')).toBeNull()
    await waitFor(() => expect(term.querySelector('textarea')).toHaveAttribute('aria-readonly', 'true'), T)
    await user.type(term.querySelector('textarea')!, 'ls{enter}')
    expect(rows()).not.toContain('dbt_project.yml  models')
  })
  it('the harness picker shows the exact command for harness and context', async () => {
    const { user } = renderApp('/ticket/DEMO-0043', { viewer: 'p_sev' })
    const d = await openDock(user)
    const sessions = await within(d).findByRole('region', { name: 'Sessions for DEMO-0043' }, T)
    const command = within(sessions).getByLabelText('Command')
    expect(command).toHaveTextContent('claude --append-system-prompt "$(orch show DEMO-0043 --section current_state)"')
    await user.click(within(sessions).getByRole('radio', { name: 'Codex' }))
    expect(command).toHaveTextContent(/^codex --config/)
    await user.click(within(sessions).getByRole('radio', { name: 'Without context (fresh window)' }))
    expect(command).toHaveTextContent(/^codex$/)
    await user.click(within(sessions).getByRole('radio', { name: 'Shell' }))
    expect(command).toHaveTextContent('$SHELL -l')
    expect(within(sessions).queryByRole('radio', { name: 'Without context (fresh window)' })).not.toBeInTheDocument()
    expect(within(sessions).getByRole('button', { name: 'Start Shell' })).toBeEnabled()
  })
  it('a new session without context opens as your own interactive window', async () => {
    const spy = vi.spyOn(api, 'runAddonAction')
    const { user } = renderApp('/ticket/DEMO-0043', { viewer: 'p_sev' })
    const d = await openDock(user)
    const sessions = await within(d).findByRole('region', { name: 'Sessions for DEMO-0043' }, T)
    await user.click(within(sessions).getByRole('radio', { name: 'Codex' }))
    await user.click(within(sessions).getByRole('radio', { name: 'Without context (fresh window)' }))
    await user.click(within(sessions).getByRole('button', { name: 'Start Codex' }))
    await waitFor(() => expect(spy).toHaveBeenCalledWith(expect.any(String), 'terminals', 'start', { harness: 'codex', context: false, ticket: 'DEMO-0043' }), T)
    const tab = await within(d).findByRole('tab', { name: /DEMO-0043 · Your Codex/ }, T)
    await waitFor(() => expect(tab).toHaveAttribute('aria-selected', 'true'), T)
    expect(tab.textContent).toMatch(/^2:codex\*/)
    await waitFor(() => expect(d.querySelector('[data-terminal-session] textarea')).not.toBeNull(), T)
    const term = Array.from(d.querySelectorAll('[data-terminal-session]')).at(-1)!
    expect(term).not.toHaveAttribute('aria-readonly')
    await waitFor(() => expect(term.querySelector('.xterm-rows')?.textContent).toContain('Fresh window: no ticket context loaded'), T)
  })
  it('resume opens a new session seeded with the earlier one\'s summary', async () => {
    const { user } = renderApp('/ticket/DEMO-0043', { viewer: 'p_sev' })
    const d = await openDock(user)
    await user.click(await within(d).findByRole('button', { name: 'Resume DEMO-0043 · Codex' }, T))
    const tab = await within(d).findByRole('tab', { name: /DEMO-0043 · Your Codex/ }, T)
    await waitFor(() => expect(tab).toHaveAttribute('aria-selected', 'true'), T)
    const term = await waitFor(() => {
      const t = Array.from(d.querySelectorAll('[data-terminal-session]')).at(-1)
      expect(t?.querySelector('.xterm-rows')?.textContent).toContain('Resumed from DEMO-0043 · Codex')
      return t!
    }, T)
    expect(term.querySelector('.xterm-rows')!.textContent).toContain('Reviewed the DEMO-0043 plan')
  })
})
