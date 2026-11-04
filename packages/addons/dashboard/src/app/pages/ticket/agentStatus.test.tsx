// Run 8, item 8: a claim the host says is not live (ended by Done, lapsed) is history, not "is working".
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { TicketDocument } from '@/api/types'
import { AgentStatus } from './Header'
import type { Viewer } from './shared'

const viewer = { name: (p: string) => p } as unknown as Viewer
const doc = (live: boolean | null) =>
  ({ tasks_state: [], claim: { agent: 'claude-code', session: 's_1', for: 'Severin', grant: 'g', since: '2026-10-23T20:22:00Z', expires: '', live } }) as unknown as TicketDocument

describe('AgentStatus', () => {
  it('a live claim says who is working', () => {
    render(<AgentStatus ticket={doc(true)} viewer={viewer} />)
    expect(screen.getByTestId('agent-status')).toHaveTextContent('is working for Severin')
  })
  it('an unknown liveness still says who is working (the host did not say otherwise)', () => {
    render(<AgentStatus ticket={doc(null)} viewer={viewer} />)
    expect(screen.getByTestId('agent-status')).toHaveTextContent('is working for Severin')
  })
  it('an ended or lapsed claim reads "No agent is working"', () => {
    render(<AgentStatus ticket={doc(false)} viewer={viewer} />)
    expect(screen.getByTestId('agent-status')).toHaveTextContent('No agent is working on this ticket')
  })
  it('a live claim while the ticket needs the person says "is waiting for you", not "is working"', () => {
    render(<AgentStatus ticket={doc(true)} viewer={viewer} needsYou />)
    const t = screen.getByTestId('agent-status')
    expect(t).toHaveTextContent('is waiting for you')
    expect(t).not.toHaveTextContent('is working')
    expect(t).not.toHaveTextContent('since') // run17 P06: the card and the pill say when; the line does not repeat it
  })
  it('a working agent keeps its "since <time>"', () => {
    render(<AgentStatus ticket={doc(true)} viewer={viewer} />)
    expect(screen.getByTestId('agent-status')).toHaveTextContent('since')
  })
})
