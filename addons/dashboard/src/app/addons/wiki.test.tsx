import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderApp } from '@/test/renderApp'
import { openTicketPanel } from '@/test/ticketPanels'

// Ticket-rail tests render at 1440 px (the rail is a column from 1280 px; below, the Panels sheet).
afterEach(() => vi.unstubAllGlobals())

const T = { timeout: 4000 }

/** Opens a page from the list: its title is the button. */
async function openPage(user: ReturnType<typeof renderApp>['user'], title: string) {
  const open = await screen.findByRole('button', { name: title }, T)
  await waitFor(() => expect(open).toBeEnabled(), T)
  await user.click(open)
}

describe('wiki page', () => {
  it('lists pages in tabs: Pages, Recently updated and Linked to tickets; titles open pages, no Open buttons', async () => {
    const { user } = renderApp('/addon/wiki/pages', { viewer: 'p_sev' })
    expect(await screen.findByRole('tab', { name: /Pages\s*7/ }, T)).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('button', { name: 'On-call runbook' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Open' })).not.toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'Linked tickets' })).toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: /^Recently updated/ }))
    expect(await screen.findByText(/5 of 7 pages changed in the last 14 days/, {}, T)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reconciliation tolerance' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Imported from old wiki' })).not.toBeInTheDocument() // 30 days old: on the Pages tab only
    await user.click(screen.getByRole('tab', { name: /Linked to tickets/ }))
    expect(await screen.findByRole('link', { name: 'DEMO-0042' })).toHaveAttribute('href', '/w/DEMO/ticket/DEMO-0042')
    expect(screen.getByRole('button', { name: 'dbt model naming' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Glossary' })).not.toBeInTheDocument() // linked to no ticket
  })
  it('opening a page: breadcrumb, title first, one meta line, ticket backlinks with titles, On this page links', async () => {
    const { user } = renderApp('/addon/wiki/pages', { viewer: 'p_sev' })
    await openPage(user, 'Tariff data conventions')
    expect(await screen.findByRole('heading', { name: 'Tariff data conventions' }, T)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'All pages' })).toBeInTheDocument()
    expect(await screen.findByText(/^by Mara · updated \d+ days? ago$/, {}, T)).toBeInTheDocument()
    expect(screen.queryAllByRole('heading', { name: 'Tariff data conventions' })).toHaveLength(1) // the body does not repeat the title
    const back = screen.getByRole('link', { name: 'DEMO-0041' })
    expect(back).toHaveAttribute('href', '/w/DEMO/ticket/DEMO-0041')
    expect(within(back.closest('tr')!).getByText('Add billing reconciliation tests')).toBeInTheDocument()
    expect(screen.queryByRole('tab')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'All pages' }))
    expect(await screen.findByRole('tab', { name: /Pages/ }, T)).toBeInTheDocument()
  })
  it('On this page: core gives headings their own ids and the links scroll to them', async () => {
    const { user, container } = renderApp('/addon/wiki/pages', { viewer: 'p_sev' })
    await openPage(user, 'Tariff data conventions')
    await screen.findByRole('heading', { name: 'Rules' }, T)
    expect(container.querySelector('h2#addon-h-rules')).not.toBeNull()
    expect(container.querySelector('h2#addon-h-loader-query')).not.toBeNull()
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    const nav = await screen.findByRole('navigation', { name: 'On this page' }, T)
    await user.click(within(nav).getByRole('button', { name: 'Checks' }))
    expect(scroll).toHaveBeenCalled()
    expect(document.getElementById('addon-h-checks')).not.toBeNull()
  })
  it('edits in place: Save changes and Cancel side by side, an unsaved-changes mark, saving returns to reading', async () => {
    const { user, container } = renderApp('/addon/wiki/pages', { viewer: 'p_sev' })
    await openPage(user, 'On-call runbook')
    await screen.findByRole('heading', { name: 'On-call runbook' }, T)
    expect(screen.queryByLabelText('Markdown')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Edit page' }))
    const body = await screen.findByLabelText('Markdown', {}, T)
    expect(screen.queryByRole('button', { name: 'Edit page' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Done editing' })).not.toBeInTheDocument()
    const save = screen.getByRole('button', { name: 'Save changes' })
    expect(save.parentElement).toBe(screen.getByRole('button', { name: 'Cancel' }).parentElement)
    expect(screen.queryByText('Unsaved changes')).not.toBeInTheDocument()
    await user.clear(body)
    await user.type(body, 'Page the secondary after 15 minutes.')
    expect(await screen.findByText('Unsaved changes')).toBeInTheDocument()
    await user.click(save)
    await waitFor(() => expect(container.textContent).toContain('Page the secondary after 15 minutes.'), T)
    expect(screen.queryByLabelText('Markdown')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Edit page' })).toBeInTheDocument()
  })
  it('leaving with unsaved edits asks first: All pages and Cancel both do; Keep editing stays', async () => {
    const { user } = renderApp('/addon/wiki/pages', { viewer: 'p_sev' })
    await openPage(user, 'Glossary')
    await user.click(await screen.findByRole('button', { name: 'Edit page' }, T))
    const body = await screen.findByLabelText('Markdown', {}, T)
    await user.type(body, ' more')
    await user.click(screen.getByRole('button', { name: 'All pages' }))
    const ask = await screen.findByRole('alertdialog', {}, T)
    expect(ask).toHaveTextContent(/unsaved edits/)
    await user.click(within(ask).getByRole('button', { name: 'Cancel' }))
    expect((screen.getByLabelText('Markdown') as HTMLTextAreaElement).value).toContain('more')
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await user.click(await within(await screen.findByRole('alertdialog')).findByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(screen.queryByLabelText('Markdown')).not.toBeInTheDocument(), T)
    expect(screen.getByRole('heading', { name: 'Glossary' })).toBeInTheDocument()
  })
  it('Cancel without edits leaves at once, without asking', async () => {
    const { user } = renderApp('/addon/wiki/pages', { viewer: 'p_sev' })
    await openPage(user, 'Glossary')
    await user.click(await screen.findByRole('button', { name: 'Edit page' }, T))
    await screen.findByLabelText('Markdown', {}, T)
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByLabelText('Markdown')).not.toBeInTheDocument(), T)
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })
  it('searches in one inline field; a search with no match offers Clear search', async () => {
    const { user } = renderApp('/addon/wiki/pages', { viewer: 'p_sev' })
    await screen.findByText('On-call runbook', {}, T)
    await user.type(screen.getByLabelText('Search pages'), 'glossary')
    await user.click(screen.getByRole('button', { name: 'Search' }))
    await waitFor(() => expect(screen.queryByText('On-call runbook')).not.toBeInTheDocument(), T)
    expect(screen.getAllByText('Glossary').length).toBeGreaterThan(0)
    expect(screen.getByRole('tab', { name: /Pages\s*7/ })).toBeInTheDocument()
    await user.clear(screen.getByLabelText('Search pages'))
    await user.type(screen.getByLabelText('Search pages'), 'zzzz')
    await user.click(screen.getByRole('button', { name: 'Search' }))
    expect(await screen.findByText('No pages match "zzzz".', {}, T)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Clear search' }))
    expect(await screen.findByText('On-call runbook', {}, T)).toBeInTheDocument()
  })
  it('New page (members): a title creates the page and opens it for editing', async () => {
    const { user } = renderApp('/addon/wiki/pages', { viewer: 'p_sev' })
    await user.click(await screen.findByRole('button', { name: 'New page' }, T))
    await user.type(await screen.findByLabelText(/^Page title/, {}, T), 'Release checklist')
    await user.click(screen.getByRole('button', { name: 'Create page' }))
    expect(await screen.findByLabelText('Markdown', {}, T)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeInTheDocument()
  })
  it('renders the imported page inert: no script, no handlers, no javascript: or data: links, no ids from the addon', async () => {
    const { user, container } = renderApp('/addon/wiki/pages', { viewer: 'p_sev' })
    await openPage(user, 'Imported from old wiki')
    await screen.findByRole('heading', { name: 'Imported from old wiki' }, T)
    await waitFor(() => expect(screen.getByText('x')).toBeInTheDocument(), T)
    const md = container.querySelector('.addon-md')!
    expect(md.querySelector('script')).toBeNull()
    expect(md.querySelector('img')).toBeNull()
    expect(md.innerHTML).not.toMatch(/\son\w+=/i)
    expect(md.innerHTML).not.toMatch(/javascript:|data:text/i)
    for (const a of md.querySelectorAll('a')) expect(a.getAttribute('href')).toMatch(/^https?:\/\//)
  })
  it('a viewer can open pages and search, but has no New page and Edit page is disabled', async () => {
    const { user } = renderApp('/addon/wiki/pages', { viewer: 'p_tom' })
    await screen.findByRole('button', { name: 'Glossary' }, T)
    expect(screen.queryByRole('button', { name: 'New page' })).not.toBeInTheDocument()
    await openPage(user, 'Glossary')
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Glossary' })).toBeInTheDocument(), T)
    const edit = screen.getByRole('button', { name: 'Edit page' })
    expect(edit).toBeDisabled()
    expect(edit.closest('[title]')).toHaveAttribute('title', 'Viewers cannot do this.') // the reason, on hover
    expect(edit).toHaveAccessibleDescription('Viewers cannot do this.')
    expect(screen.getByRole('button', { name: 'All pages' })).toBeEnabled()
    expect(screen.queryByLabelText('Markdown')).not.toBeInTheDocument()
  })
})

describe('wiki viewer actions follow the installed version', () => {
  it('an installed update that removed the viewer search action disables Search for a viewer, opening a page stays enabled', async () => {
    renderApp('/addon/wiki/pages', {
      viewer: 'p_tom',
      setup: (s) => {
        const w = s.addons.find((a) => a.name === 'wiki')!
        w.update = { version: w.version, capabilities: [], package_sha256: w.package_sha256, changelog: 'x', actions: { save_settings: { minRole: 'owner' }, open: { minRole: 'viewer', label: 'Open page' } } }
      },
    })
    const open = await screen.findByRole('button', { name: 'Glossary' }, T)
    await waitFor(() => expect(open).toBeEnabled(), T)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Search' })).toBeDisabled(), T)
  })
})

describe('wiki ticket panel', () => {
  it('shows the pages linked to DEMO-0043 and links another one', async () => {
    vi.stubGlobal('innerWidth', 1440)
    const { user } = renderApp('/ticket/DEMO-0043', { viewer: 'p_sev' })
    const panel = await screen.findByRole('complementary', { name: 'Ticket details' }, T)
    await openTicketPanel(user, 'Related pages')
    const frame = await waitFor(() => {
      const f = panel.querySelector('[data-addon="wiki"]')
      expect(f).not.toBeNull()
      return f as HTMLElement
    }, T)
    const titles = () => within(frame).getAllByRole('listitem').map((li) => li.textContent ?? '')
    await waitFor(() => expect(titles().some((t) => t.includes('Reconciliation tolerance'))).toBe(true), T)
    expect(titles().some((t) => t.includes('Glossary'))).toBe(false)
    await user.selectOptions(within(frame).getByLabelText('Page'), 'Glossary')
    await user.click(within(frame).getByRole('button', { name: 'Link page' }))
    await waitFor(() => expect(titles().some((t) => t.includes('Glossary'))).toBe(true), T)
  })
})

describe('wiki unsaved edits and the router', () => {
  it('a palette navigation away from unsaved edits asks first; Keep editing stays, Discard goes', async () => {
    const { user } = renderApp('/addon/wiki/pages', { viewer: 'p_sev' })
    await openPage(user, 'Glossary')
    await user.click(await screen.findByRole('button', { name: 'Edit page' }, T))
    await user.type(await screen.findByLabelText('Markdown', {}, T), ' more')
    await screen.findByText('Unsaved changes')
    await user.keyboard('{Meta>}k{/Meta}')
    await user.keyboard('Board{Enter}')
    expect(await screen.findByRole('dialog', { name: 'Discard unsaved changes?' }, T)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Keep editing' }))
    expect((screen.getByLabelText('Markdown') as HTMLTextAreaElement).value).toContain('more')
    await user.keyboard('{Meta>}k{/Meta}')
    await user.keyboard('Board{Enter}')
    await user.click(await screen.findByRole('button', { name: 'Discard changes' }, T))
    expect(await screen.findByRole('heading', { name: 'Board' }, T)).toBeInTheDocument()
  })
  it('without edits the palette just navigates', async () => {
    const { user } = renderApp('/addon/wiki/pages', { viewer: 'p_sev' })
    await openPage(user, 'Glossary')
    await user.click(await screen.findByRole('button', { name: 'Edit page' }, T))
    await screen.findByLabelText('Markdown', {}, T)
    await user.keyboard('{Meta>}k{/Meta}')
    await user.keyboard('Board{Enter}')
    expect(await screen.findByRole('heading', { name: 'Board' }, T)).toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: 'Discard unsaved changes?' })).not.toBeInTheDocument()
  })
})
