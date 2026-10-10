import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { renderApp } from '@/test/renderApp'

// Ticket-rail tests render at 1440 px (the rail is a column from 1280 px; below, the Panels sheet).
afterEach(() => vi.unstubAllGlobals())

describe('app shell', () => {
  it('renders nav links, the addon group with badges, and the Today placeholder', async () => {
    renderApp('/')
    expect(await screen.findByRole('heading', { name: 'Today' })).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: /Board/ })).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: /Code reviews/ })).toBeInTheDocument()
    // The sidebar entry (Today's Glance also has "Open Apps & shares").
    expect(await screen.findByRole('link', { name: /^Apps & shares/ })).toBeInTheDocument()
    expect((await screen.findAllByRole('img', { name: /From addon:/ })).length).toBeGreaterThanOrEqual(5)
    expect(await screen.findByText('agents granted until 18:00')).toBeInTheDocument()
  })

  it('Pin to sidebar says what happened: already pinned, or pinned', async () => {
    const msg = vi.spyOn(toast, 'message')
    const ok = vi.spyOn(toast, 'success')
    renderApp('/')
    await screen.findByRole('heading', { name: 'Today' })
    await screen.findByRole('link', { name: /Code reviews/ })
    window.dispatchEvent(new CustomEvent('orch:pin-addon-page', { detail: 'github/reviews' }))
    expect(msg).toHaveBeenCalledWith('Already pinned')
    expect(ok).not.toHaveBeenCalledWith('Pinned to the sidebar')
    msg.mockRestore()
    ok.mockRestore()
  })

  it('dismisses toasts when the route changes, and not before', async () => {
    const dismiss = vi.spyOn(toast, 'dismiss')
    const { user } = renderApp('/')
    await screen.findByRole('heading', { name: 'Today' })
    expect(dismiss).not.toHaveBeenCalled()
    const ok = toast.success('Saved')
    const bad = toast.error('Could not save', { duration: Infinity })
    await user.click(await screen.findByRole('link', { name: /Board/ }))
    await screen.findByRole('heading', { name: 'Board' })
    // The confirmation goes with the route; the failure stays until dismissed.
    await waitFor(() => expect(dismiss).toHaveBeenCalledWith(ok))
    expect(dismiss).not.toHaveBeenCalledWith(bad)
    dismiss.mockRestore()
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

  it('Skip to content is the first Tab stop and moves focus to main', async () => {
    const { user } = renderApp('/')
    await screen.findByRole('heading', { name: 'Today' })
    await user.tab()
    const skip = screen.getByRole('link', { name: 'Skip to content' })
    expect(skip).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(document.getElementById('main')).toHaveFocus()
  })

  it('shows at most 6 addon links and a More addons button listing all, each with its A and a unique icon', async () => {
    const { user } = renderApp('/')
    await screen.findByRole('heading', { name: 'Today' })
    const nav = await screen.findByRole('navigation', { name: 'Main' })
    const more = await within(nav).findByRole('button', { name: /More addons \(\d+\)/ })
    const links = within(nav).getAllByRole('link').filter((l) => l.getAttribute('href')?.includes('/addon/'))
    expect(links.length).toBeLessThanOrEqual(6)
    // The resolved icon component draws a lucide-<name> class: two addons sharing one icon would share it.
    const icons = links.map((l) => [...(l.querySelector('svg')?.classList ?? [])].find((c) => /^lucide-/.test(c) && c !== 'lucide'))
    expect(icons.every(Boolean)).toBe(true)
    expect(new Set(icons).size).toBe(icons.length)
    await user.click(more)
    expect((await screen.findAllByRole('img', { name: /From addon:|From the .* addon/ })).length).toBeGreaterThan(links.length)
  })

  it('pins are unlimited; "More addons (n)" counts only the addons that are not pinned, and reads "All addons" when none is left', async () => {
    const { user } = renderApp('/')
    await screen.findByRole('heading', { name: 'Today' })
    const nav = await screen.findByRole('navigation', { name: 'Main' })
    const addonLinks = () => within(nav).getAllByRole('link').filter((l) => l.getAttribute('href')?.includes('/addon/'))
    const more = await within(nav).findByRole('button', { name: /^More addons \(\d+\)$/ })
    const total = addonLinks().length + Number(/\((\d+)\)/.exec(more.getAttribute('aria-label')!)![1])
    expect(total).toBeGreaterThan(6)
    await user.click(more)
    expect(await screen.findByRole('heading', { name: 'All addons in this workspace' })).toBeInTheDocument()
    // Pin one more: the sidebar holds 7 and the count goes down by one.
    const pins = () => screen.queryAllByRole('button', { name: /^Pin .* to the sidebar$/ })
    await user.click(pins()[0])
    expect(addonLinks()).toHaveLength(7)
    expect(await screen.findByRole('button', { name: `More addons (${total - 7})` })).toBeInTheDocument()
    // Pin them all: nothing is "more" any more.
    while (pins().length) await user.click(pins()[0])
    expect(addonLinks()).toHaveLength(total)
    expect(screen.getByRole('button', { name: 'All addons' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /More addons/ })).toBeNull()
  })

  it('Move ticket to… on the board offers the focused card and leaves out its current status', async () => {
    const { user } = renderApp('/board')
    const card = await screen.findByTestId('card-DEMO-0043', {}, { timeout: 15000 })
    card.focus()
    await user.keyboard('{Control>}k{/Control}')
    await user.click(await screen.findByText('Move DEMO-0043 to…'))
    const status = card.getAttribute('data-status')!
    const labels = (await screen.findAllByRole('option')).map((o) => o.textContent)
    expect(labels.length).toBeGreaterThan(0)
    expect(labels).not.toContain({ backlog: 'Backlog', open: 'Open', 'in-progress': 'In progress', waiting: 'Waiting', testing: 'Testing' }[status])
  })

  it('Today has an accessible name with its count', async () => {
    renderApp('/')
    expect(await screen.findByRole('link', { name: /^Today, \d+ need you$/ })).toBeInTheDocument()
  })

  it('Reset demo asks first', async () => {
    const { user } = renderApp('/')
    await screen.findByRole('heading', { name: 'Today' })
    await user.click(screen.getByRole('button', { name: 'Reset demo' }))
    expect(await screen.findByRole('alertdialog')).toBeInTheDocument()
  })

  it('a viewer has no New ticket button and c explains instead of opening the form', async () => {
    const { user } = renderApp('/', { viewer: 'p_tom' })
    await screen.findByRole('heading', { name: 'Today' })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Viewing as' })).toHaveTextContent(/viewer/))
    expect(screen.queryByRole('button', { name: /New ticket/ })).toBeNull()
    await user.keyboard('c')
    expect(await screen.findByText(/Viewers cannot create tickets/)).toBeInTheDocument()
    expect(screen.queryByRole('heading', { level: 1, name: 'New ticket' })).toBeNull()
  })

  it('renders an addon page with the badge in the page title', async () => {
    renderApp('/addon/usage/overview')
    const title = await screen.findByRole('heading', { level: 1, name: /Usage/ })
    expect(title.querySelector('[aria-label="From the Usage addon"]')).not.toBeNull()
    expect(await screen.findByText('CHF 31.40')).toBeInTheDocument()
  })

  it('opens the new ticket overlay from the button and from the c shortcut, but not while typing', async () => {
    const { user } = renderApp('/')
    await screen.findByRole('heading', { name: 'Today' })
    await user.click(await screen.findByRole('button', { name: /New ticket/ }))
    expect(await screen.findByRole('dialog', { name: 'New ticket' })).toBeInTheDocument()
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'New ticket' })).toBeNull())
    await user.click(screen.getByRole('link', { name: /Board/ }))
    await waitFor(() => expect(screen.getByTestId('topbar-title')).toHaveTextContent('Board'))
    await user.keyboard('c')
    const sheet = await screen.findByRole('dialog', { name: 'New ticket' })
    expect(screen.getByTestId('topbar-title')).toHaveTextContent('Board')
    await user.type(within(sheet).getByLabelText('Title'), 'c')
    expect(within(sheet).getByLabelText('Title')).toHaveValue('c')
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

    it('an exact key is the first, preselected hit: Enter opens it', async () => {
      const { user } = renderApp('/ticket/DEMO-0043')
      await screen.findByRole('heading', { level: 1 })
      await user.keyboard('{Control>}k{/Control}')
      await user.type(screen.getByPlaceholderText(/Search tickets/), 'DEMO-0041')
      const first = (await screen.findAllByRole('option'))[0]
      expect(first).toHaveTextContent('DEMO-0041')
      expect(first).toHaveAttribute('aria-selected', 'true')
      await user.keyboard('{Enter}')
      await waitFor(() => expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(/reconciliation tests|billing/i))
      expect(screen.queryByPlaceholderText(/Search tickets/)).toBeNull()
    })

    it('a key typed without the dash finds it; a query of only dashes or underscores lists nothing', async () => {
      const { user } = renderApp('/')
      await screen.findByRole('heading', { name: 'Today' })
      await user.keyboard('{Control>}k{/Control}')
      const box = screen.getByPlaceholderText(/Search tickets/)
      await user.type(box, 'demo0041')
      const first = (await screen.findAllByRole('option'))[0]
      expect(first).toHaveTextContent('DEMO-0041')
      await user.clear(box)
      await user.type(box, '-_-')
      expect(screen.queryByRole('group', { name: 'Tickets' })).toBeNull()
    })

    it('_ and - match spaces: "billing run id" finds billing_run_id', async () => {
      const { user } = renderApp('/')
      await screen.findByRole('heading', { name: 'Today' })
      await user.keyboard('{Control>}k{/Control}')
      await user.type(screen.getByPlaceholderText(/Search tickets/), 'billing run id')
      expect(await screen.findByRole('option', { name: /billing_run_id/ })).toBeInTheDocument()
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
      expect(await screen.findByRole('dialog', { name: /Give a verdict/ })).toBeInTheDocument()
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
      expect(screen.getByRole('dialog', { name: 'New ticket' })).toBeInTheDocument()
      expect(screen.getByTestId('topbar-title')).toHaveTextContent('Tickets')
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
    it('is calm: one row per workspace (prefix, name, check, needs-you count), no recent tickets, no role line', async () => {
      const { user } = renderApp('/')
      await user.click(await screen.findByRole('button', { name: 'Switch workspace' }))
      const list = await screen.findByRole('list', { name: 'Workspaces' })
      const rows = within(list).getAllByRole('listitem')
      expect(rows).toHaveLength(3)
      const demo = within(rows[0]).getByRole('button', { name: /^DEMO · Acme energy data/ })
      // "Current" is said once (aria-current), not again in the name or by the check.
      expect(demo).toHaveAttribute('aria-current', 'true')
      expect(within(list).getAllByRole('button').filter((b) => b.getAttribute('aria-current'))).toHaveLength(1)
      expect(demo.getAttribute('aria-label')).not.toMatch(/current/i)
      // The count is the same "needs you" number as the sidebar badge, shown only when there is something.
      await waitFor(() => expect(demo).toHaveAccessibleName(/^DEMO · Acme energy data, \d+ need you$/))
      for (const r of rows) {
        expect(within(r).queryAllByRole('link')).toHaveLength(0) // no recent tickets
        expect(within(r).queryByText(/^(owner|maintainer|member|viewer)$/)).toBeNull() // the role is in the tooltip
        expect(within(r).queryByLabelText(/Relay/)).toBeNull()
      }
      // The shortcut is a hint on the button (shown on hover/focus), and keyboard users get it as aria-keyshortcuts.
      expect(demo).toHaveAttribute('aria-keyshortcuts', expect.stringMatching(/^(Meta|Control)\+1$/))
    })

    it('the role and the relay are in the row\'s tooltip; arrow keys move between workspaces; Enter switches', async () => {
      const { user } = renderApp('/')
      await user.click(await screen.findByRole('button', { name: 'Switch workspace' }))
      const list = await screen.findByRole('list', { name: 'Workspaces' })
      const [demo, int] = within(list).getAllByRole('button')
      await waitFor(() => expect(demo).toHaveFocus())
      // Opening the switcher focuses the current row but shows no tooltip next to it.
      await new Promise((r) => setTimeout(r, 700))
      expect(screen.queryByRole('tooltip')).toBeNull()
      await user.hover(int)
      expect(await screen.findByRole('tooltip', {}, { timeout: 3000 })).toHaveTextContent(/Your role: owner.*Relay not connected/)
      await user.unhover(int)
      await user.keyboard('{ArrowDown}')
      expect(int).toHaveFocus()
      await user.keyboard('{Enter}')
      await waitFor(() => expect(screen.getByRole('button', { name: 'Switch workspace' })).toHaveTextContent(/Internal/))
    })

    it('⌘1..n still switch with the switcher closed', async () => {
      const { user } = renderApp('/')
      await screen.findByRole('button', { name: 'Switch workspace' })
      await user.keyboard('{Meta>}3{/Meta}')
      await waitFor(() => expect(screen.getByRole('button', { name: 'Switch workspace' })).toHaveTextContent(/Client VM/))
      await user.keyboard('{Meta>}1{/Meta}')
      await waitFor(() => expect(screen.getByRole('button', { name: 'Switch workspace' })).toHaveTextContent(/Acme/))
      expect(screen.queryByRole('list', { name: 'Workspaces' })).toBeNull()
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
      vi.stubGlobal('innerWidth', 1440)
      const { user } = renderApp('/ticket/DEMO-0043')
      await screen.findByRole('heading', { level: 1 })
      await user.keyboard('{Meta>}2{/Meta}')
      expect(await screen.findByRole('table', { name: 'Tickets' })).toBeInTheDocument()
      expect(await screen.findByText('DEMO-0043 is in Acme energy data')).toBeInTheDocument()
    })

    it('opening a ticket of another workspace (palette Recent) switches to its home workspace', async () => {
      vi.stubGlobal('innerWidth', 1440)
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
      expect(await screen.findByText('Switched to Acme energy data to open DEMO-0043')).toBeInTheDocument()
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
