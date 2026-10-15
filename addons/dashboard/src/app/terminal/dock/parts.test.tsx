// The dock's parts rendered alone (no app, no xterm): fast checks of what they show and offer.
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { sessionName } from '@/api/harnesses'
import type { TerminalSessionView } from '@/api/terminals'
import { TooltipProvider } from '@/components/ui/tooltip'
import { sessionMode } from '../SessionStrip'
import { NewSessionForm } from './NewSession'
import { SessionBrowser } from './SessionBrowser'
import { SessionTabs } from './SessionTabs'

const wrap = (ui: ReactNode) => render(<TooltipProvider>{ui}</TooltipProvider>)

const session = (over: Partial<TerminalSessionView> = {}): TerminalSessionView => ({
  id: 's1',
  label: 'DEMO-0043 · Your shell',
  kind: 'person',
  owner: 'p_sev',
  ticket: 'DEMO-0043',
  started: '2026-10-09T09:12:00Z',
  status: 'running',
  interactive: true,
  ctx: { user: 'severin', cwd: '~/energy', branch: 'main', owner: 'person', now: '2026-10-09T11:30:00Z', cursor: 1, grant: null, claim: null, ticket: null },
  transcript: [],
  harness: 'shell',
  purpose: null,
  command: '$SHELL -l',
  context: false,
  summary: null,
  resumedFrom: null,
  ...over,
})

describe('names and modes', () => {
  it('names sessions by purpose', () => {
    expect(sessionName({ harness: 'claude', kind: 'agent' })).toBe('Claude · Agent')
    expect(sessionName({ harness: 'claude', kind: 'person' })).toBe('Claude · Yours')
    expect(sessionName({ harness: 'codex', kind: 'agent', purpose: 'Review' })).toBe('Codex · Review')
    expect(sessionName({ harness: 'shell', kind: 'person' })).toBe('Shell')
    expect(sessionName({ harness: 'gemini', kind: 'agent' })).toBe('Unsupported harness gemini')
  })
  it('says what you can do in a session', () => {
    expect(sessionMode(session({ harness: 'claude' }), true)).toBe('Your Claude Code · Type a prompt')
    expect(sessionMode(session(), true)).toBe('Your shell · Type a command')
    expect(sessionMode(session({ kind: 'agent', interactive: false }), false)).toBe('Watching agent · Read only')
    expect(sessionMode(session({ status: 'stopped' }), false)).toBe('Ended · Transcript')
  })
})

describe('New session form', () => {
  it('starts with the last harness, offers where and the summary option, and shows the command on request', async () => {
    const onStart = vi.fn()
    const user = userEvent.setup()
    wrap(<NewSessionForm ticket="DEMO-0043" lastHarness="codex" canStart onStart={onStart} />)
    const select = screen.getByRole('combobox', { name: 'Harness' })
    expect(select).toHaveValue('codex')
    expect(screen.queryByLabelText('Command')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Show command' }))
    expect(screen.getByLabelText('Command')).toHaveTextContent(/^codex --config instructions=.*DEMO-0043/)
    await user.click(screen.getByRole('checkbox', { name: "Include the ticket's current-state summary" }))
    expect(screen.getByLabelText('Command')).toHaveTextContent(/^codex$/)
    await user.click(screen.getByRole('button', { name: 'Start Codex' }))
    expect(onStart).toHaveBeenLastCalledWith({ harness: 'codex', inTicket: true, summary: false })
    await user.click(screen.getByRole('radio', { name: 'Workspace' }))
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument() // the summary belongs to the ticket worktree
    await user.selectOptions(select, 'shell')
    await user.click(screen.getByRole('button', { name: 'Start Shell' }))
    expect(onStart).toHaveBeenLastCalledWith({ harness: 'shell', inTicket: false, summary: false })
  })
  it('a shell has no summary option; outside a ticket there is no Where; viewers cannot start', () => {
    const { unmount } = wrap(<NewSessionForm ticket="DEMO-0043" lastHarness="shell" canStart onStart={() => {}} />)
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    unmount()
    wrap(<NewSessionForm lastHarness="nope" canStart={false} onStart={() => {}} />)
    expect(screen.getByRole('combobox', { name: 'Harness' })).toHaveValue('shell')
    expect(screen.queryByRole('radiogroup', { name: 'Where' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Start/ })).toBeDisabled()
    expect(screen.getByText('Viewers can watch agent sessions but cannot start one.')).toBeInTheDocument()
  })
})

describe('Session browser', () => {
  const ended = session({ id: 'e1', status: 'stopped', kind: 'agent', harness: 'codex', purpose: 'Review', interactive: false, summary: 'Reviewed the plan.' })
  it('has Running and Earlier tabs; continuing says what it does', async () => {
    const onContinue = vi.fn()
    const user = userEvent.setup()
    wrap(<SessionBrowser title="Sessions for DEMO-0043" running={[session()]} ended={[ended]} names={sessionName} current={null} canStart onSelect={() => {}} onContinue={onContinue} />)
    expect(screen.getByRole('tab', { name: 'Running (1)' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('button', { name: 'Join Shell' })).toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: 'Earlier (1)' }))
    expect(screen.getByRole('button', { name: 'View transcript of Codex · Review' })).toBeInTheDocument()
    expect(screen.getByText('Starts a new Codex session using this summary.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Continue from summary of Codex · Review' }))
    expect(onContinue).toHaveBeenCalledWith('e1')
  })
  it('an unsupported harness offers its transcript only', async () => {
    const user = userEvent.setup()
    wrap(<SessionBrowser title="x" running={[]} ended={[{ ...ended, harness: 'gemini' }]} names={sessionName} current={null} canStart onSelect={() => {}} onContinue={() => {}} />)
    await user.click(screen.getByRole('tab', { name: 'Earlier (1)' }))
    expect(screen.getByText('Unsupported harness gemini: transcript only.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Continue from summary/ })).not.toBeInTheDocument()
  })
})

describe('Session tabs', () => {
  const windows = Array.from({ length: 5 }, (_, i) => ({ session: session({ id: `w${i}`, status: i === 4 ? 'stopped' : 'running' }), index: i, name: `Win ${i}` }))
  it('shows what fits, keeps the current one visible, puts the rest under "+n"; transcripts close', async () => {
    const onClose = vi.fn()
    const onSelect = vi.fn()
    const user = userEvent.setup()
    wrap(<SessionTabs windows={windows} current="w4" visible={2} onSelect={onSelect} onClose={onClose} />)
    const tabs = within(screen.getByRole('tablist', { name: 'Session windows' })).getAllByRole('tab')
    expect(tabs.map((t) => t.textContent)).toEqual(['0Win 0, Running (simulated)', '4Win 4, Ended'])
    expect(tabs[1]).toHaveAttribute('aria-selected', 'true')
    await user.click(screen.getByRole('button', { name: 'Close transcript Win 4' }))
    expect(onClose).toHaveBeenCalledWith('w4')
    await user.click(screen.getByRole('button', { name: '3 more sessions' }))
    await user.click(await screen.findByRole('menuitem', { name: /Win 2/ }))
    expect(onSelect).toHaveBeenCalledWith('w2')
  })
})
