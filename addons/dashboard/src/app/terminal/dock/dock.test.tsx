// The dock in the app: open/collapse/resize/side, the page beside it, the ticket's sessions, starting and continuing.
// Kept to what needs the whole app; the parts are tested alone in parts.test.tsx.
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 8000 }
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

type User = ReturnType<typeof renderApp>['user']
const bar = () => screen.findByRole('button', { name: /^Open terminal dock/ }, T)
const dock = () => screen.findByRole('region', { name: 'Terminal dock' }, T)
const openDock = async (user: User) => {
  await user.click(await bar())
  return dock()
}
const menu = async (user: User, d: HTMLElement, item: string | RegExp) => {
  await user.click(within(d).getByRole('button', { name: 'Dock menu' }))
  return screen.findByRole('menuitem', { name: item })
}
const prefs = (viewer = 'p_sev') => JSON.parse(localStorage.getItem(`orch.dock.${viewer}`) ?? 'null')
/** Keep localStorage across renders (renderApp clears it) and start from these prefs. */
const withPrefs = (p: object) => {
  vi.spyOn(Storage.prototype, 'clear').mockImplementation(() => {})
  localStorage.setItem('orch.dock.p_sev', JSON.stringify({ side: 'bottom', open: false, bottom: 280, right: 440, harness: 'claude', ...p }))
}

describe('terminal dock: open, collapse, resize, side', () => {
  it('a full-width 32 px bar; Ctrl+` opens with focus on the session strip; Collapse returns focus to the bar', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev' })
    expect((await bar()).closest('aside')).toHaveStyle({ height: '32px' })
    expect(await bar()).toHaveTextContent('Ctrl+`')
    fireEvent.keyDown(window, { key: '`', code: 'Backquote', ctrlKey: true })
    const d = await dock()
    await waitFor(() => expect(document.activeElement).toHaveAttribute('data-session-strip'), T)
    await user.click(await menu(user, d, /Collapse/))
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: /^Open terminal dock/ })), T)
    expect(prefs().open).toBe(false)
  })
  it('sits beside the page (never over it), resizes by keyboard and drag within its limits, and remembers the size', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev' })
    const d = await openDock(user)
    const main = document.querySelector('main')!
    expect(d.contains(main)).toBe(false)
    expect(d.className).not.toMatch(/\b(fixed|absolute)\b/)
    const handle = within(d).getByRole('separator', { name: 'Resize terminal dock' })
    expect(handle).toHaveAttribute('title', 'Drag to resize · Arrow keys')
    expect(handle).toHaveAttribute('aria-valuenow', '280')
    await waitFor(() => expect(document.activeElement).toHaveAttribute('data-session-strip'), T) // opening focused the strip
    handle.focus()
    await user.keyboard('{ArrowUp}')
    expect(handle).toHaveAttribute('aria-valuenow', '296')
    await user.keyboard('{Shift>}{ArrowDown}{/Shift}')
    expect(handle).toHaveAttribute('aria-valuenow', '232')
    await user.keyboard('{Home}')
    expect(handle).toHaveAttribute('aria-valuenow', '160')
    await user.keyboard('{End}')
    expect(handle).toHaveAttribute('aria-valuenow', '630')
    fireEvent.pointerDown(handle, { clientY: 600, pointerId: 1 })
    fireEvent.pointerMove(handle, { clientY: 700, pointerId: 1 })
    fireEvent.pointerUp(handle, { pointerId: 1 })
    expect(handle).toHaveAttribute('aria-valuenow', '530')
    expect(prefs().bottom).toBe(530)
  })
  it('on the right the ticket page lays out for its own width: Panels instead of the rail, tabs scroll in their row', async () => {
    withPrefs({ open: true, side: 'right' })
    renderApp('/ticket/DEMO-0043', { viewer: 'p_sev' })
    const d = await dock()
    expect(d).toHaveAttribute('data-dock-side', 'right')
    // 1440 window, 232 px sidebar: page + dock = 1208; the page keeps 640, so the dock is at most 568.
    expect(within(d).getByRole('separator')).toHaveAttribute('aria-valuemax', '568')
    expect(d).toHaveStyle({ width: '440px' })
    expect(await screen.findByRole('button', { name: /^Panels \(/ }, T)).toBeInTheDocument()
    expect(screen.queryByRole('complementary', { name: 'Ticket details' })).not.toBeInTheDocument()
    const tabs = document.querySelector('[data-scroll-tabs]')!
    expect(tabs.className).toMatch(/overflow-x-auto/)
    expect(tabs.className).toMatch(/min-w-0/)
  })
  it('without room on the right it docks at the bottom and the menu says so', async () => {
    withPrefs({ open: true, side: 'right' })
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ width: 792, height: 700, top: 0, left: 0, right: 792, bottom: 700, x: 0, y: 0, toJSON: () => ({}) })
    const { user } = renderApp('/', { viewer: 'p_sev' })
    const d = await dock()
    expect(d).toHaveAttribute('data-dock-side', 'bottom')
    expect(await menu(user, d, 'Not enough room — dock at the bottom')).toHaveAttribute('aria-disabled', 'true')
  })
  it('the side survives a reload', async () => {
    withPrefs({})
    const first = renderApp('/', { viewer: 'p_sev' })
    const d = await openDock(first.user)
    await first.user.click(await menu(first.user, d, 'Move to the right'))
    await waitFor(() => expect(d).toHaveAttribute('data-dock-side', 'right'), T)
    expect(prefs()).toMatchObject({ side: 'right', open: true })
    first.unmount()
    renderApp('/', { viewer: 'p_sev' })
    expect(await dock()).toHaveAttribute('data-dock-side', 'right')
    localStorage.removeItem('orch.dock.p_sev')
  })
  it('is not there where terminals may not use pty, and goes away when the addon is turned off', async () => {
    withPrefs({ open: true })
    renderApp('/', { viewer: 'p_sev' })
    await dock()
    const ws = (await api.getWorkspaces())[0].id
    await act(async () => {
      await api.postAddonOp(ws, 'terminals', { op: 'disable' })
      window.dispatchEvent(new Event('visibilitychange')) // the app refetches what changed (as on returning to the tab)
    })
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Terminal dock' })).not.toBeInTheDocument(), T)
    expect(screen.queryByRole('button', { name: /^Open terminal dock/ })).not.toBeInTheDocument()
    fireEvent.keyDown(window, { key: '`', code: 'Backquote', ctrlKey: true })
    expect(screen.queryByRole('region', { name: 'Terminal dock' })).not.toBeInTheDocument()
    localStorage.removeItem('orch.dock.p_sev')
  })
})

describe('terminal dock on a ticket page', () => {
  it('lists this ticket\'s sessions only, by purpose, with Running and Earlier', async () => {
    const { user } = renderApp('/ticket/DEMO-0043', { viewer: 'p_sev' })
    const d = await openDock(user)
    expect(within(d).getByText('DEMO-0043', { selector: '[data-dock-scope]' })).toBeInTheDocument()
    const browser = await within(d).findByRole('region', { name: 'Sessions for DEMO-0043' }, T)
    expect(within(browser).getByRole('button', { name: 'Join Shell' })).toBeInTheDocument()
    expect(within(browser).getByRole('button', { name: 'Watch Claude · Agent (read only)' })).toBeInTheDocument()
    expect(within(browser).queryByText(/Scratch|DEMO-0043 Shell/)).not.toBeInTheDocument()
    await user.click(within(browser).getByRole('tab', { name: 'Earlier (1)' }))
    expect(within(browser).getByText(/Reviewed the DEMO-0043 plan/)).toBeInTheDocument()
    const tabs = within(within(d).getByRole('tablist', { name: 'Session windows' })).getAllByRole('tab')
    expect(tabs.map((t) => t.textContent)).toEqual(['0Shell, Running (simulated)', '1Claude · Agent, Read only'])
  })
  it('a viewer watches agent sessions only and cannot start or continue', async () => {
    const { user } = renderApp('/ticket/DEMO-0043', { viewer: 'p_tom' })
    const d = await openDock(user)
    const browser = await within(d).findByRole('region', { name: 'Sessions for DEMO-0043' }, T)
    await within(browser).findByRole('button', { name: 'Watch Claude · Agent (read only)' }, T)
    expect(within(browser).queryByRole('button', { name: /^Join/ })).not.toBeInTheDocument()
    await user.click(within(browser).getByRole('tab', { name: 'Earlier (1)' }))
    expect(within(browser).getByRole('button', { name: /Continue from summary/ })).toBeDisabled()
    await user.click(within(d).getByRole('button', { name: 'New session' }))
    expect(await screen.findByRole('button', { name: /^Start/ }, T)).toBeDisabled()
  })
  it('the workspace dock names the ticket of each session', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev' })
    const d = await openDock(user)
    expect(within(d).getByText('Workspace', { selector: '[data-dock-scope]' })).toBeInTheDocument()
    await user.click(within(d).getByRole('button', { name: 'Sessions' }))
    const browser = await within(d).findByRole('region', { name: 'Sessions in this workspace' }, T)
    expect(within(browser).getByRole('button', { name: 'Join DEMO-0043 Shell' })).toBeInTheDocument()
    expect(within(browser).getByRole('button', { name: 'Watch DEMO-0043 Claude · Agent (read only)' })).toBeInTheDocument()
  })
  it('the workspace dock leaves out sessions of tickets the viewer cannot see', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev', setup: (s) => void vi.spyOn(s, 'isVisible').mockImplementation((key: string) => key !== 'DEMO-0043') })
    const d = await openDock(user)
    await user.click(within(d).getByRole('button', { name: 'Sessions' }))
    const browser = await within(d).findByRole('region', { name: 'Sessions in this workspace' }, T)
    expect(browser.textContent).not.toMatch(/DEMO-0043/)
    expect(d.querySelector('header')!.textContent).not.toMatch(/DEMO-0043/)
    expect(within(d).getByText('No session open')).toBeInTheDocument() // both running sessions are on DEMO-0043
  })
  it('watching an agent is read only: the strip says so, a footer offers your own session, its CLI goes through clean()', async () => {
    const { user } = renderApp('/ticket/DEMO-0043', {
      viewer: 'p_sev',
      setup: (s) => {
        const def = (s as unknown as { defs: Map<string, { title: string }> }).defs.get('DEMO-0043')!
        def.title = 'Seeds \x1b]8;;https://evil.example\x07click\x1b]8;;\x07 \x9b31m \x1b[5n end'
      },
    })
    const d = await openDock(user)
    await user.click(await within(d).findByRole('button', { name: 'Watch Claude · Agent (read only)' }, T))
    expect(within(d).queryByRole('region', { name: 'Sessions for DEMO-0043' })).not.toBeInTheDocument() // the browser closed
    const strip = await within(d).findByRole('group', { name: /^Session: 1 Claude · Agent · Watching agent · Read only/ }, T)
    expect(within(strip).getByText('Simulated')).toBeInTheDocument()
    expect(within(strip).getByText('Running (simulated)')).toBeInTheDocument()
    expect(within(d).getByText("Read only — this is the agent's session.")).toBeInTheDocument()
    expect(within(d).getByRole('button', { name: 'Start your own session' })).toBeInTheDocument()
    const term = await waitFor(() => {
      const t = d.querySelector('[data-terminal-session="agent1"]')
      expect(t).not.toBeNull()
      return t!
    }, T)
    expect(term).toHaveAttribute('aria-readonly', 'true')
    const rows = () => term.querySelector('.xterm-rows')?.textContent ?? ''
    // It ends on what DEMO-0043 really waits on: Severin's answer to Q2 (its plan is approved).
    await waitFor(() => expect(rows()).toContain('Waiting for Q2'), T)
    expect(rows()).toContain('waiting · Answer Q2 · Severin')
    expect(rows()).not.toContain('human_only')
    expect(rows()).not.toMatch(/esc to interrupt|│ >/)
    expect(rows()).not.toMatch(/[\u001b\u0080-\u009f]/)
    expect(term.querySelector('a[href]')).toBeNull()
  })
  it('New session without the summary opens your own window where you can type', async () => {
    const spy = vi.spyOn(api, 'runAddonAction')
    const { user } = renderApp('/ticket/DEMO-0043', { viewer: 'p_sev' })
    const d = await openDock(user)
    await user.click(within(d).getByRole('button', { name: 'New session' }))
    const form = await screen.findByRole('form', { name: 'New session' }, T)
    await user.selectOptions(within(form).getByRole('combobox', { name: 'Harness' }), 'codex')
    await user.click(within(form).getByRole('checkbox', { name: "Include the ticket's current-state summary" }))
    await user.click(within(form).getByRole('button', { name: 'Start Codex' }))
    await waitFor(() => expect(spy).toHaveBeenCalledWith(expect.any(String), 'terminals', 'start', { harness: 'codex', context: false, ticket: 'DEMO-0043' }), T)
    const tab = await within(d).findByRole('tab', { name: /Codex · Yours/ }, T)
    await waitFor(() => expect(tab).toHaveAttribute('aria-selected', 'true'), T)
    expect(within(d).getByRole('group', { name: /Your Codex · Type a prompt/ })).toBeInTheDocument()
    expect(prefs().harness).toBe('codex') // remembered for next time
  })
  it('a session started in the workspace from a ticket page takes the dock to the workspace scope, with the way back', async () => {
    const spy = vi.spyOn(api, 'runAddonAction')
    const { user } = renderApp('/ticket/DEMO-0043', { viewer: 'p_sev' })
    const d = await openDock(user)
    await user.click(within(d).getByRole('button', { name: 'New session' }))
    const form = await screen.findByRole('form', { name: 'New session' }, T)
    await user.selectOptions(within(form).getByRole('combobox', { name: 'Harness' }), 'shell')
    await user.click(within(form).getByRole('radio', { name: 'Workspace' }))
    await user.click(within(form).getByRole('button', { name: 'Start Shell' }))
    await waitFor(() => expect(spy).toHaveBeenCalledWith(expect.any(String), 'terminals', 'start', { harness: 'shell', context: false }), T)
    const dd = await dock()
    await waitFor(() => expect(within(dd).getByText('Workspace', { selector: '[data-dock-scope]' })).toBeInTheDocument(), T)
    // The new scratch shell is listed and selected (it has no ticket, so the ticket scope would have hidden it).
    await waitFor(() => expect(within(dd).getByRole('tab', { selected: true })).toHaveTextContent(/^\d+Shell/), T)
    expect(within(dd).getByRole('tab', { selected: true })).not.toHaveTextContent('DEMO-0043')
    await user.click(within(dd).getByRole('button', { name: 'Back to DEMO-0043' }))
    // The dock body is per scope (a new element): ask for it again.
    await waitFor(async () => expect(within(await dock()).getByText('DEMO-0043', { selector: '[data-dock-scope]' })).toBeInTheDocument(), T)
  })
  it('toasts sit above an open bottom dock', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev' })
    await bar()
    expect(document.documentElement.style.getPropertyValue('--dock-bottom')).toBe('32px')
    await openDock(user)
    expect(document.documentElement.style.getPropertyValue('--dock-bottom')).toBe('280px')
    const { toast } = await import('sonner')
    act(() => void toast.success('Saved.'))
    const toaster = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('[data-sonner-toaster]')
      expect(el).not.toBeNull()
      return el!
    }, T)
    expect(toaster.getAttribute('style')).toContain('var(--dock-bottom, 0px)')
  })
  it('collapse keeps the selection and opened transcripts; Continue from summary opens a seeded session', async () => {
    const { user } = renderApp('/ticket/DEMO-0043', { viewer: 'p_sev' })
    let d = await openDock(user)
    const browser = await within(d).findByRole('region', { name: 'Sessions for DEMO-0043' }, T)
    await user.click(within(browser).getByRole('tab', { name: 'Earlier (1)' }))
    await user.click(within(browser).getByRole('button', { name: 'View transcript of Codex · Review' }))
    await within(d).findByRole('group', { name: /Ended · Transcript/ }, T)
    fireEvent.keyDown(window, { key: '`', code: 'Backquote', ctrlKey: true })
    await bar()
    fireEvent.keyDown(window, { key: '`', code: 'Backquote', ctrlKey: true })
    d = await dock()
    expect(await within(d).findByRole('tab', { name: /Codex · Review/ }, T)).toHaveAttribute('aria-selected', 'true')
    await user.click(within(d).getByRole('button', { name: 'Sessions' }))
    const again = await within(d).findByRole('region', { name: 'Sessions for DEMO-0043' }, T)
    await user.click(within(again).getByRole('tab', { name: 'Earlier (1)' }))
    await user.click(within(again).getByRole('button', { name: 'Continue from summary of Codex · Review' }))
    const tab = await within(d).findByRole('tab', { name: /Codex · Yours/ }, T)
    await waitFor(() => expect(tab).toHaveAttribute('aria-selected', 'true'), T)
    await waitFor(() => expect(Array.from(d.querySelectorAll('.xterm-rows')).map((r) => r.textContent).join('')).toContain('Continued from DEMO-0043 · Codex'), T)
  })
})
