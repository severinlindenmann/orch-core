import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 4000 }

describe('guide page', () => {
  it('shows Getting around, opens Keyboard shortcuts and lists g b', async () => {
    const { user, container } = renderApp('/addon/guide/guide', { viewer: 'p_sev' })
    expect(await screen.findByRole('heading', { name: 'Getting around' }, T)).toBeInTheDocument()
    expect(screen.getByText('Current')).toBeInTheDocument() // the chip marks the open page and does not echo the "Open" button
    const item = screen.getByText('Keyboard shortcuts').closest('li')!
    await user.click(within(item).getByRole('button', { name: 'Open' }))
    await waitFor(() => expect(container.querySelector('.addon-md')).toHaveTextContent('g b'), T)
  })
  it('a viewer can open pages', async () => {
    const { user } = renderApp('/addon/guide/guide', { viewer: 'p_tom' })
    const item = (await screen.findByText('Questions', {}, T)).closest('li')!
    const open = within(item).getByRole('button', { name: 'Open' })
    await waitFor(() => expect(open).toBeEnabled(), T)
    await user.click(open)
    await screen.findByRole('heading', { name: 'Questions' }, T)
  })
})

describe('? opens the Help sheet', () => {
  it('on the board it shows Tickets and sections; Esc closes it', async () => {
    const { user } = renderApp('/board', { viewer: 'p_sev' })
    await waitFor(() => expect(screen.getByTestId('topbar-title')).toHaveTextContent('Board'), T)
    await user.keyboard('?')
    const sheet = await screen.findByRole('dialog', { name: 'Help' }, T)
    expect(await within(sheet).findByRole('heading', { name: 'Tickets and sections' }, T)).toBeInTheDocument()
    const frame = sheet.querySelector('section[data-addon="guide"]')!
    expect(frame).not.toBeNull()
    expect(within(frame as HTMLElement).getByRole('img', { name: /From addon: guide/ })).toBeInTheDocument()
    expect(within(frame as HTMLElement).queryByRole('link', { name: 'Open the guide' })).toBeNull()
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Help' })).not.toBeInTheDocument())
  })
  it('on a ticket it shows Gates and approvals, and Open the guide goes to the full page', async () => {
    const { user } = renderApp('/ticket/DEMO-0041', { viewer: 'p_tom' })
    await screen.findByRole('heading', { level: 1 }, T)
    await user.keyboard('?')
    const sheet = await screen.findByRole('dialog', { name: 'Help' }, T)
    await within(sheet).findByRole('heading', { name: 'Gates and approvals' }, T)
    await user.click(within(sheet).getByRole('link', { name: 'Open the guide' }))
    expect(await screen.findByRole('heading', { name: 'Gates and approvals' }, T)).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('topbar-title')).toHaveTextContent('Guide'), T)
    expect(screen.queryByRole('dialog', { name: 'Help' })).not.toBeInTheDocument()
  })
  it('inside a text input it types a ? instead', async () => {
    const { user } = renderApp('/tickets/new', { viewer: 'p_sev' })
    const title = await screen.findByLabelText('Title', {}, T)
    await user.type(title, 'Why?')
    expect(title).toHaveValue('Why?')
    expect(screen.queryByRole('dialog', { name: 'Help' })).not.toBeInTheDocument()
  })
})
