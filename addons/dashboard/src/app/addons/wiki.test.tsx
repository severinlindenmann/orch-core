import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 4000 }

describe('wiki page', () => {
  it('lists pages, opens one, edits and saves it', async () => {
    const { user, container } = renderApp('/addon/wiki/pages', { viewer: 'p_sev' })
    const item = (await screen.findByText('On-call runbook', {}, T)).closest('li')!
    await user.click(within(item).getByRole('button', { name: 'Open' }))
    await waitFor(() => expect(screen.getByRole('heading', { name: 'On-call runbook' })).toBeInTheDocument(), T)
    const body = await screen.findByLabelText('Markdown', {}, T)
    await user.clear(body)
    await user.type(body, 'Page the secondary after 15 minutes.')
    await user.click(screen.getByRole('button', { name: 'Save page' }))
    await waitFor(() => expect(container.querySelector('.addon-md')).toHaveTextContent('Page the secondary after 15 minutes.'), T)
  })
  it('searches the list', async () => {
    const { user } = renderApp('/addon/wiki/pages', { viewer: 'p_sev' })
    await screen.findByText('On-call runbook', {}, T)
    await user.type(screen.getByLabelText('Search'), 'glossary')
    await user.click(screen.getByRole('button', { name: 'Search' }))
    await waitFor(() => expect(screen.queryByText('On-call runbook')).not.toBeInTheDocument(), T)
    expect(screen.getAllByText('Glossary').length).toBeGreaterThan(0)
  })
  it('renders the imported page inert: no script, no handlers, no javascript: or data: links', async () => {
    const { user, container } = renderApp('/addon/wiki/pages', { viewer: 'p_sev' })
    const item = (await screen.findByText('Imported from old wiki', {}, T)).closest('li')!
    await user.click(within(item).getByRole('button', { name: 'Open' }))
    await screen.findByRole('heading', { name: 'Imported from old wiki' }, T)
    await waitFor(() => expect(screen.getByText('x')).toBeInTheDocument(), T)
    const md = container.querySelector('.addon-md')!
    expect(md.querySelector('script')).toBeNull()
    expect(md.querySelector('img')).toBeNull()
    expect(md.innerHTML).not.toMatch(/\son\w+=/i)
    expect(md.innerHTML).not.toMatch(/javascript:|data:text/i)
    for (const a of md.querySelectorAll('a')) expect(a.getAttribute('href')).toMatch(/^https?:\/\//)
  })
  it('shows the page title and who edited it last above the text', async () => {
    renderApp('/addon/wiki/pages', { viewer: 'p_sev' })
    expect(await screen.findByText(/^by Mara · updated \d+d ago$/, {}, T)).toBeInTheDocument()
  })
  it('a viewer can open pages and search, but the edit form is read-only', async () => {
    const { user } = renderApp('/addon/wiki/pages', { viewer: 'p_tom' })
    const item = (await screen.findByText('Glossary', {}, T)).closest('li')!
    const open = within(item).getByRole('button', { name: 'Open' })
    await waitFor(() => expect(open).toBeEnabled(), T)
    await user.click(open)
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Glossary' })).toBeInTheDocument(), T)
    expect(screen.getByRole('button', { name: 'Search' })).toBeEnabled()
    expect(screen.getByLabelText('Markdown')).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Save page' })).toBeDisabled()
  })
})

describe('wiki viewer actions follow the installed version', () => {
  it('an installed update that removed the viewer search action disables Search for a viewer, Open stays enabled', async () => {
    renderApp('/addon/wiki/pages', {
      viewer: 'p_tom',
      setup: (s) => {
        const w = s.addons.find((a) => a.name === 'wiki')!
        w.update = { version: w.version, capabilities: [], package_sha256: w.package_sha256, changelog: 'x', actions: { save_settings: { minRole: 'owner' }, open: { minRole: 'viewer', label: 'Open page' } } }
      },
    })
    const item = (await screen.findByText('Glossary', {}, T)).closest('li')!
    await waitFor(() => expect(within(item).getByRole('button', { name: 'Open' })).toBeEnabled(), T)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Search' })).toBeDisabled(), T)
  })
})

describe('wiki ticket panel', () => {
  it('shows the pages linked to DEMO-0043 and links another one', async () => {
    const { user } = renderApp('/ticket/DEMO-0043', { viewer: 'p_sev' })
    const panel = await screen.findByRole('complementary', { name: 'Ticket details' }, T)
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
