import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ReaderLevels } from './Readers'

const hub = vi.hoisted(() => ({ data: undefined as unknown, isPending: false, fetchStatus: 'idle' }))
vi.mock('../hub/shared', () => ({ useHub: () => hub }))
vi.mock('@/api/features', () => ({ hasFeature: () => true }))
vi.mock('@tanstack/react-router', () => ({ Link: ({ children, ...p }: { children: unknown }) => <a {...p}>{children as never}</a> }))
vi.mock('@tanstack/react-query', async (original) => ({
  ...(await original<typeof import('@tanstack/react-query')>()),
  useQuery: (o: { queryKey: unknown[] }) => {
    if (o.queryKey[0] === 'click-status') return { data: { device: 'd1' } }
    if (o.queryKey[0] === 'readers') return { data: { available: true, readers: [{ device: 'd1', person: 'p', level: 1, reader: 1 }], required_reader: 1 } }
    return { data: [] }
  },
}))

afterEach(() => cleanup())

describe('Settings > Devices, only this computer approves', () => {
  it('no hub: the line says to connect a hub first, with the link', () => {
    hub.data = { joined: false, mode: 'standalone' }
    render(<ReaderLevels ws="w1" />)
    expect(screen.getByTestId('only-this-computer')).toHaveTextContent('Only this computer approves for now')
    expect(screen.getByTestId('only-this-computer')).toHaveTextContent('connect a hub first')
    expect(screen.getByTestId('readers-connect-hub')).toBeInTheDocument()
  })

  it('connected to a hub: no "connect a hub first" line and no link (R14b)', () => {
    hub.data = { joined: true, mode: 'selective' }
    render(<ReaderLevels ws="w1" />)
    expect(screen.getByTestId('only-this-computer')).toHaveTextContent('Only this computer approves for now')
    expect(document.body.textContent).not.toMatch(/connect a hub/i)
    expect(screen.queryByTestId('readers-connect-hub')).toBeNull()
  })

  it('the hub is still being asked: no stale line flashes', () => {
    hub.data = undefined
    hub.isPending = true
    hub.fetchStatus = 'fetching'
    render(<ReaderLevels ws="w1" />)
    expect(document.body.textContent).not.toMatch(/connect a hub/i)
    hub.isPending = false
    hub.fetchStatus = 'idle'
  })
})
