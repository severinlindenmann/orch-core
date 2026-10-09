import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
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

  describe('workspace switcher', () => {
    it('shows needs-you previews per workspace and opens one directly', async () => {
      const { user } = renderApp('/')
      await user.click(await screen.findByRole('button', { name: 'Switch workspace' }))
      const int = await screen.findByRole('group', { name: /INT/ })
      await user.click((await within(int).findAllByRole('link'))[0])
      // Today's h1 is still on screen until the navigation lands, so wait for the ticket heading.
      await waitFor(() => expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Rotate shared Slack webhook'))
      // The ticket page's h1 is the title (the key is shown beside it), so the INT ticket is identified by its title.
      expect(screen.getAllByText('INT-0007').length).toBeGreaterThan(0)
      expect(screen.getByRole('button', { name: 'Switch workspace' })).toHaveTextContent(/Internal/)
    })

    it('shows role, needs-you count and a muted relay dot per workspace', async () => {
      const { user } = renderApp('/')
      await user.click(await screen.findByRole('button', { name: 'Switch workspace' }))
      const demo = await screen.findByRole('group', { name: /DEMO/ })
      expect(within(demo).getByText('owner')).toBeInTheDocument()
      expect(within(demo).getByLabelText(/\d+ need you/)).toBeInTheDocument()
      expect(within(demo).getByLabelText('Relay not connected')).toBeInTheDocument()
    })

    it('keeps the board when switching (waits on the topbar title: Board has no h1)', async () => {
      const { user } = renderApp('/board')
      await waitFor(() => expect(screen.getByTestId('topbar-title')).toHaveTextContent('Board'))
      await screen.findByRole('button', { name: 'Switch workspace' })
      await user.keyboard('{Meta>}2{/Meta}')
      await waitFor(() => expect(screen.getByRole('button', { name: 'Switch workspace' })).toHaveTextContent(/Internal/))
      expect(screen.getByTestId('topbar-title')).toHaveTextContent('Board')
    })

    it('does not switch with the number shortcut while typing', async () => {
      const { user } = renderApp('/tickets/new')
      const title = await screen.findByRole('textbox', { name: /title/i })
      await user.click(title)
      await user.keyboard('{Meta>}2{/Meta}')
      expect(screen.getByRole('button', { name: 'Switch workspace' })).toHaveTextContent(/Acme/)
    })

    it('leaves a ticket page for the list when switching, with a toast naming the ticket workspace', async () => {
      const { user } = renderApp('/ticket/DEMO-0043')
      await screen.findByRole('heading', { level: 1 })
      await user.keyboard('{Meta>}2{/Meta}')
      expect(await screen.findByRole('table', { name: 'Tickets' })).toBeInTheDocument()
      expect(await screen.findByText('DEMO-0043 is in Acme energy data')).toBeInTheDocument()
    })

    it('opening a ticket of another workspace (palette Recent) switches to its home workspace', async () => {
      const { user } = renderApp('/ticket/DEMO-0043')
      await screen.findByRole('heading', { level: 1 })
      await user.keyboard('{Meta>}2{/Meta}')
      expect(await screen.findByRole('table', { name: 'Tickets' })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Switch workspace' })).toHaveTextContent(/Internal/)
      await user.keyboard('{Control>}k{/Control}')
      const recent = await screen.findByRole('group', { name: 'Recent' })
      await user.click(within(recent).getByRole('option', { name: /DEMO-0043/ }))
      await screen.findByRole('heading', { level: 1, name: /Load tariff tables/ })
      await waitFor(() => expect(screen.getByRole('button', { name: 'Switch workspace' })).toHaveTextContent(/Acme/))
      // DEMO's addons (Publish is not installed in INT) render in the rail.
      await waitFor(() => expect(screen.getByRole('complementary', { name: 'Ticket details' }).querySelector('[data-addon="publish"]')).not.toBeNull())
    })

    it('leaves an addon page the target workspace has not enabled, with a toast', async () => {
      const { user } = renderApp('/addon/usage/overview')
      await screen.findByRole('heading', { level: 1, name: /Usage/ })
      await user.keyboard('{Meta>}2{/Meta}')
      expect(await screen.findByRole('heading', { name: 'Today' })).toBeInTheDocument()
      expect(await screen.findByText(/Usage is not enabled in Internal/)).toBeInTheDocument()
    })

    it('remembers the workspace across reloads', async () => {
      const { user } = renderApp('/agents')
      await screen.findByRole('button', { name: 'Switch workspace' })
      await user.keyboard('{Meta>}3{/Meta}')
      await waitFor(() => expect(screen.getByRole('button', { name: 'Switch workspace' })).toHaveTextContent(/Client VM/))
      expect(localStorage.getItem('orch.workspace')).toMatch(/^c7d2b9e4/)
    })

    it('the palette lists the shortcut and switches with the same function', async () => {
      const { user } = renderApp('/board')
      await screen.findByRole('button', { name: 'Switch workspace' })
      await user.keyboard('{Control>}k{/Control}')
      const opt = await screen.findByRole('option', { name: /Switch to INT/ })
      expect(within(opt).getByText(/^(⌘|Ctrl\+)2$/)).toBeInTheDocument()
      await user.click(opt)
      await waitFor(() => expect(screen.getByRole('button', { name: 'Switch workspace' })).toHaveTextContent(/Internal/))
      expect(screen.getByTestId('topbar-title')).toHaveTextContent('Board')
    })

    it('palette ticket actions follow the viewer role in the ticket workspace, not the current one', async () => {
      // Tom is a viewer in DEMO (the current workspace) but a member in CLI.
      const { user } = renderApp('/ticket/CLI-0003', { viewer: 'p_tom' })
      await screen.findByRole('heading', { level: 1 })
      await user.keyboard('{Control>}k{/Control}')
      expect(await screen.findByRole('option', { name: /Comment/ })).toBeInTheDocument()
    })

    it('Today renders its option buttons without React key warnings', async () => {
      const err = vi.spyOn(console, 'error').mockImplementation(() => {})
      renderApp('/')
      await screen.findByRole('heading', { name: 'Today' })
      await screen.findAllByTestId(/^card-/)
      expect(err.mock.calls.filter((c) => String(c[0]).includes('unique "key"'))).toEqual([])
      err.mockRestore()
    })
  })
})

describe('viewer menu', () => {
  it('names each person with their role, and the choice survives a dataset switch', async () => {
    const { user } = renderApp('/')
    await user.click(await screen.findByRole('button', { name: 'Viewing as' }))
    expect(await screen.findByRole('menuitemradio', { name: 'Severin · owner' })).toBeInTheDocument()
    expect(screen.getByRole('menuitemradio', { name: 'Tom · viewer' })).toBeInTheDocument()
    await user.click(screen.getByRole('menuitemradio', { name: 'Mara · maintainer' }))
    await user.click(await screen.findByRole('button', { name: 'Busy day' }))
    await user.click(await screen.findByRole('button', { name: 'Switch to busy day' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Busy day' })).toHaveAttribute('aria-pressed', 'true'))
    expect(await screen.findByRole('button', { name: 'Viewing as' })).toHaveTextContent('Mara')
  })
})
