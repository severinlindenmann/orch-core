// Run 16 P01: an agent that waits for the person (a question or permission prompt the host detects) is shown, with a way in.
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { TicketDocument } from '@/api/types'

const openDockOn = vi.fn()
vi.mock('@/app/terminal/dock/request', () => ({ openDockOn: (r: string) => openDockOn(r) }))

import { AgentStatus, ProgressPill } from './Header'
import type { Viewer } from './shared'

const viewer = { name: (p: string) => p } as unknown as Viewer
const noClaim = { key: 'AT-4', status: 'in_progress', claim: null, tasks_state: [] } as unknown as TicketDocument
const start = { run: vi.fn() } as never

beforeEach(() => openDockOn.mockClear())

describe('ticket header while the agent waits (P01)', () => {
  it('shows Needs you, "Agent is waiting for you" and Open agent, which opens the run', () => {
    render(<ProgressPill phase={{ kind: 'waiting', run: 'r_9' }} start={start} ticket={noClaim} viewer={viewer} />)
    const pill = screen.getByTestId('progress-pill')
    expect(pill).toHaveTextContent('Needs you')
    expect(pill).toHaveTextContent('Agent is waiting for you')
    fireEvent.click(screen.getByRole('button', { name: 'Open agent' }))
    expect(openDockOn).toHaveBeenCalledWith('r_9')
  })
  it('a live run past 90 s says it takes longer but offers no Try again', () => {
    render(<ProgressPill phase={{ kind: 'starting', slow: true, live: true }} start={start} ticket={noClaim} viewer={viewer} />)
    expect(screen.getByTestId('progress-pill')).toHaveTextContent('Taking longer than usual')
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
  })
  it('a slow start with no live run still offers Try again', () => {
    render(<ProgressPill phase={{ kind: 'starting', slow: true }} start={start} ticket={noClaim} viewer={viewer} />)
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
  })
  it('the status line with a wait and no claim never says "No agent is working"', () => {
    render(<AgentStatus ticket={noClaim} viewer={viewer} wait="r_9" needsYou />)
    const t = screen.getByTestId('agent-status')
    expect(t).toHaveTextContent('Claude is waiting for you')
    expect(t).not.toHaveTextContent('No agent is working')
    fireEvent.click(screen.getByRole('button', { name: 'Open agent' }))
    expect(openDockOn).toHaveBeenCalledWith('r_9')
  })
  it('a live run without a claim reads as starting, not "No agent is working"', () => {
    render(<AgentStatus ticket={noClaim} viewer={viewer} liveRun />)
    expect(screen.getByTestId('agent-status')).not.toHaveTextContent('No agent is working')
  })
})
