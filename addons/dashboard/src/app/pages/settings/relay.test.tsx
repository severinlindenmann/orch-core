import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 6000 }

describe('Settings > Relay & devices (Preview, simulated)', () => {
  it('says it is a simulated preview and lists the devices with key epoch and last seen', async () => {
    renderApp('/settings/relay')
    expect(await screen.findByText(/Everything on this tab is simulated/, {}, T)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Relay & devices' })).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Not connected')
    const mac = screen.getByText("Severin's MacBook Pro").closest('tr')!
    expect(within(mac).getByText('This device')).toBeInTheDocument()
    expect(within(mac).getByText('epoch 1')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Pair a device' })).toBeDisabled()
    expect(within(screen.getByRole('list', { name: 'Sync queue' })).getAllByText('Queued').length).toBeGreaterThan(0)
  })
  it('Connect is signed, then the link comes online and pairing works end to end', async () => {
    const { user } = renderApp('/settings/relay')
    await user.click(await screen.findByRole('button', { name: 'Connect' }, T))
    await user.click(await screen.findByRole('button', { name: 'Sign and connect' }, T))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Online'), T)
    await user.click(screen.getByRole('button', { name: 'Pair a device' }))
    const dialog = await screen.findByRole('dialog', { name: /Pair a device/ }, T)
    expect(within(dialog).getByRole('img', { name: /Mock QR code/ })).toBeInTheDocument()
    expect(within(dialog).getByText(/Valid for/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: /Simulate: a phone scans/ }))
    expect(await within(dialog).findByText(/wants to join/, {}, T)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Codes match' }))
    await user.click(await screen.findByRole('button', { name: 'Sign and add device' }, T))
    expect(await screen.findByText("Severin's iPad", {}, T)).toBeInTheDocument()
  })
  it('removing a device is signed and starts a new epoch', async () => {
    const { user } = renderApp('/settings/relay')
    const row = (await screen.findByText("Tom's iPad", {}, T)).closest('tr')!
    await user.click(within(row).getByRole('button', { name: 'Remove' }))
    const sign = await screen.findByRole('dialog', { name: /Remove Tom's iPad/ }, T)
    expect(within(sign).getByText(/Starts epoch 2/)).toBeInTheDocument()
    await user.click(within(sign).getByRole('button', { name: 'Sign and remove' }))
    await waitFor(() => expect(screen.queryByText("Tom's iPad")).not.toBeInTheDocument(), T)
    expect(await screen.findByText(/epoch 2 · since/, {}, T)).toBeInTheDocument()
  })
  it('a maintainer reads it without the owner actions', async () => {
    renderApp('/settings/relay', { viewer: 'p_mara' })
    expect(await screen.findByText("Tom's iPad", {}, T)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Connect' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Pair a device' })).not.toBeInTheDocument()
  })
})
