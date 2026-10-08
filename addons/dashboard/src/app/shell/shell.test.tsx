import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderApp } from '@/test/renderApp'

describe('app shell', () => {
  it('renders nav links, the addon group with badges, and the Today placeholder', async () => {
    renderApp('/')
    expect(await screen.findByRole('heading', { name: 'Today' })).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: /Board/ })).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: /Code reviews/ })).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: /Apps & shares/ })).toBeInTheDocument()
    expect((await screen.findAllByRole('img', { name: /From addon:/ })).length).toBeGreaterThanOrEqual(5)
    expect(await screen.findByText('agents granted until 18:00')).toBeInTheDocument()
  })

  it('opens the command palette with Ctrl+K and lists actions and addon commands', async () => {
    const { user } = renderApp('/')
    await screen.findByRole('heading', { name: 'Today' })
    await user.keyboard('{Control>}k{/Control}')
    expect(await screen.findByPlaceholderText(/Search tickets/)).toBeInTheDocument()
    expect(await screen.findByText('New ticket', { selector: '[cmdk-item] *, [cmdk-item]' })).toBeInTheDocument()
    expect(await screen.findByText('Refresh pull requests')).toBeInTheDocument()
  })

  it('toggles the sidebar between wide and narrow with the button and with [', async () => {
    const { user } = renderApp('/')
    await screen.findByRole('heading', { name: 'Today' })
    const aside = document.querySelector('aside')!
    const start = aside.getAttribute('data-collapsed')
    await user.click(screen.getByRole('button', { name: /(Expand|Collapse) sidebar/ }))
    expect(aside.getAttribute('data-collapsed')).not.toBe(start)
    await user.keyboard('[[')
    expect(aside.getAttribute('data-collapsed')).toBe(start)
  })

  it('renders an addon page with the badge in the page title', async () => {
    renderApp('/addon/usage/overview')
    const title = await screen.findByRole('heading', { level: 1, name: /Usage/ })
    expect(title.querySelector('[aria-label="From addon: usage"]')).not.toBeNull()
    expect(await screen.findByText('CHF 31.40')).toBeInTheDocument()
  })

  it('opens the new ticket page from the button and from the c shortcut, but not while typing', async () => {
    const { user } = renderApp('/')
    await screen.findByRole('heading', { name: 'Today' })
    await user.click(screen.getByRole('button', { name: /New ticket/ }))
    expect(await screen.findByRole('heading', { level: 1, name: 'New ticket' })).toBeInTheDocument()
    await user.click(screen.getByRole('link', { name: /Board/ }))
    await waitFor(() => expect(screen.getByTestId('topbar-title')).toHaveTextContent('Board'))
    await user.keyboard('c')
    expect(await screen.findByRole('heading', { level: 1, name: 'New ticket' })).toBeInTheDocument()
    await user.type(screen.getByLabelText('Title'), 'c')
    expect(screen.getByLabelText('Title')).toHaveValue('c')
  })

  describe('full palette', () => {
    it('finds a ticket by title and opens it', async () => {
      const { user } = renderApp('/')
      await screen.findByRole('heading', { name: 'Today' })
      await user.keyboard('{Control>}k{/Control}')
      await user.type(screen.getByPlaceholderText(/Search tickets/), 'billing')
      await user.click(await screen.findByRole('option', { name: /DEMO-0043/ }))
      expect(await screen.findByRole('heading', { level: 1, name: /tariff tables/i })).toBeInTheDocument()
    })

    it('offers ticket actions only on a ticket page, and opens the sign dialog for approve', async () => {
      const { user } = renderApp('/ticket/DEMO-0041')
      await screen.findByRole('heading', { level: 1 })
      await user.keyboard('{Control>}k{/Control}')
      const group = await screen.findByRole('group', { name: 'On this ticket' })
      expect(group).toBeInTheDocument()
      const approve = within(group).queryAllByRole('option', { name: /^Give verdict/ })
      expect(approve.length).toBeGreaterThan(0)
      await user.click(approve[0])
      expect(await screen.findByRole('button', { name: /Sign with Touch ID/ })).toBeInTheDocument()
    })

    it('hides ticket actions on other pages and for a viewer', async () => {
      const { user } = renderApp('/', { viewer: 'p_tom' })
      await screen.findByRole('heading', { name: 'Today' })
      await user.keyboard('{Control>}k{/Control}')
      await screen.findByPlaceholderText(/Search tickets/)
      expect(screen.queryByRole('group', { name: 'On this ticket' })).toBeNull()
    })

    it('shows no ticket actions to a viewer on a ticket page', async () => {
      const { user } = renderApp('/ticket/DEMO-0043', { viewer: 'p_tom' })
      await screen.findByRole('heading', { level: 1 })
      await user.keyboard('{Control>}k{/Control}')
      await screen.findByPlaceholderText(/Search tickets/)
      expect(screen.queryByRole('group', { name: 'On this ticket' })).toBeNull()
    })

    it('> limits the list to commands', async () => {
      const { user } = renderApp('/')
      await screen.findByRole('heading', { name: 'Today' })
      await user.keyboard('{Control>}k{/Control}')
      await user.type(screen.getByPlaceholderText(/Search tickets/), '>')
      expect(screen.queryByRole('group', { name: 'Tickets' })).toBeNull()
      expect(screen.getByRole('group', { name: 'Addon commands' })).toBeInTheDocument()
    })

    it('remembers recently visited tickets', async () => {
      const { user } = renderApp('/ticket/DEMO-0043')
      await screen.findByRole('heading', { level: 1 })
      await user.keyboard('{Control>}k{/Control}')
      expect(within(await screen.findByRole('group', { name: 'Recent' })).getByText(/DEMO-0043/)).toBeInTheDocument()
    })

    it('g then b goes to the board, and g keys are ignored while typing', async () => {
      const { user } = renderApp('/')
      await screen.findByRole('heading', { name: 'Today' })
      await user.keyboard('gb')
      await waitFor(() => expect(screen.getByTestId('topbar-title')).toHaveTextContent('Board'))
      await user.keyboard('gl')
      expect(await screen.findByRole('heading', { name: 'Tickets' })).toBeInTheDocument()
      await user.click(screen.getByRole('button', { name: /New ticket/ }))
      await user.type(await screen.findByLabelText('Title'), 'ga')
      expect(screen.getByLabelText('Title')).toHaveValue('ga')
      expect(screen.getByRole('heading', { level: 1, name: 'New ticket' })).toBeInTheDocument()
    })

    it('shows shortcuts next to items and opens the save view dialog from Create', async () => {
      const { user } = renderApp('/')
      await screen.findByRole('heading', { name: 'Today' })
      await user.keyboard('{Control>}k{/Control}')
      expect(within(await screen.findByRole('option', { name: /Go to Board/ })).getByText('g b')).toBeInTheDocument()
      await user.click(await screen.findByRole('option', { name: /Save view/ }))
      expect(await screen.findByRole('dialog', { name: 'Save view' })).toBeInTheDocument()
    })

    it('@ lists people and opens Tickets filtered by that person', async () => {
      const { user } = renderApp('/')
      await screen.findByRole('heading', { name: 'Today' })
      await user.keyboard('{Control>}k{/Control}')
      await user.type(screen.getByPlaceholderText(/Search tickets/), '@mara')
      await user.click(await screen.findByRole('option', { name: /Mara/ }))
      expect(await screen.findByRole('heading', { name: 'Tickets' })).toBeInTheDocument()
    })
  })
})
