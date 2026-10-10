import { act, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
import type { MockStore } from '@/mocks/store'
import { renderApp } from '@/test/renderApp'
import { artifactKey, artifactUrlId, parseArtifactKey, previewTarget } from './selection'

const T = { timeout: 4000 }

afterEach(() => vi.restoreAllMocks())

const rowOf = (name: string) => screen.getByText(name, { selector: 'span' }).closest('[data-artifact]') as HTMLElement

describe('Artifacts page', () => {
  it('is a core nav entry after Tickets', async () => {
    renderApp('/')
    const nav = await screen.findByRole('navigation', { name: 'Main' })
    const names = within(nav).getAllByRole('link').map((l) => l.getAttribute('aria-label') ?? l.textContent)
    expect(names.indexOf('Artifacts')).toBe(names.indexOf('Tickets') + 1)
  })
  it('lists the workspace artifacts with ticket, adder and time; filters show their counts', async () => {
    const { user } = renderApp('/artifacts')
    expect(await screen.findByText('12 artifacts', {}, T)).toBeInTheDocument()
    const row = screen.getByRole('button', { name: 'Preview tariff-export.log' }).closest('tr')!
    expect(within(row).getByRole('link', { name: 'DEMO-0043' })).toBeInTheDocument()
    expect(within(row).getByText('Claude Code for Severin')).toBeInTheDocument()
    for (const f of ['Kind', 'Ticket', 'Added by', 'Added']) expect(screen.getByRole('combobox', { name: f })).toBeInTheDocument()
    // Narrowing and clearing (the menus are Radix selects, exercised in the browser pass; the host filters are in mocks/artifacts.test.ts).
    await user.type(screen.getByRole('searchbox', { name: 'Search artifacts' }), '.log')
    expect(await screen.findByText('4 artifacts', {}, T)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(await screen.findByText('12 artifacts', {}, T)).toBeInTheDocument()
  })
  it('searches by name', async () => {
    const { user } = renderApp('/artifacts')
    await screen.findByText('12 artifacts', {}, T)
    await user.type(screen.getByRole('searchbox', { name: 'Search artifacts' }), 'tolerance')
    expect(await screen.findByText('1 artifact', {}, T)).toBeInTheDocument()
  })
  it('switches to the grid and remembers it per person', async () => {
    const { user, unmount } = renderApp('/artifacts')
    await screen.findByText('12 artifacts', {}, T)
    await user.click(screen.getByRole('radio', { name: 'Grid' }))
    expect(await screen.findByRole('list', { name: 'Artifacts' })).toBeInTheDocument()
    expect(localStorage.getItem('orch.artifacts.view.p_sev')).toBe('grid')
    unmount()
  })
  it('Tom does not see the artifacts of a restricted ticket (the host leaves them out)', async () => {
    const setup = (s: MockStore) => {
      s.append('DEMO-0044', { type: 'artifact.added', actor: 'p_sev', name: 'incident-notes.md', kind: 'report', bytes: 900, sha256: 'a'.repeat(64) })
    }
    renderApp('/artifacts', { viewer: 'p_tom', setup })
    expect(await screen.findByText('12 artifacts', {}, T)).toBeInTheDocument()
    expect(screen.queryByText('incident-notes.md')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /incident-notes/ })).toBeNull()
  })
})

// Owner request 2026-10-10 (G3): what is clickable, and what each target does. Here the page is narrow (jsdom
// measures 0, no wide window), so a preview opens as the drawer.
describe('Artifacts page: one action per item', () => {
  for (const view of ['list', 'grid'] as const) {
    it(`${view}: Preview opens the drawer; name, label and row are plain text`, async () => {
      const { user } = renderApp('/artifacts', { storage: { 'orch.artifacts.view.p_sev': view } })
      const preview = await screen.findByRole('button', { name: 'Preview tariff-export.log' }, T)
      expect(preview).toHaveTextContent('Preview')
      const item = preview.closest('[data-artifact]') as HTMLElement
      // The name is not a button or a link; the only targets are the ticket link and the action.
      expect(within(item).getByText('tariff-export.log').closest('a, button')).toBeNull()
      expect(within(item).getAllByRole('link').map((l) => l.textContent)).toEqual(['DEMO-0043'])
      expect(within(item).getAllByRole('button')).toEqual([preview])
      await user.click(preview)
      const sheet = await screen.findByRole('dialog', {}, T)
      expect(within(sheet).getByRole('link', { name: 'DEMO-0043' })).toBeInTheDocument()
      expect(await within(sheet).findByRole('button', { name: /Copy all/ }, T)).toBeInTheDocument()
      await user.keyboard('{Escape}')
      await waitFor(() => expect(preview).toHaveFocus(), T)
    })
    it(`${view}: a web link says it opens a new tab and does not preview`, async () => {
      const { user } = renderApp('/artifacts', { storage: { 'orch.artifacts.view.p_sev': view } })
      const link = await screen.findByRole('link', { name: 'Open link dbt docs: tariffs (opens in a new tab)' }, T)
      expect(link).toHaveTextContent('Open link')
      expect(link).toHaveAttribute('href', 'https://docs.acme.example/dbt/tariffs')
      expect(link).toHaveAttribute('target', '_blank')
      expect(link.getAttribute('rel')).toMatch(/noopener/)
      expect(link.getAttribute('rel')).toMatch(/noreferrer/)
      link.addEventListener('click', (e) => e.preventDefault())
      await user.click(link)
      expect(screen.queryByRole('dialog')).toBeNull()
      expect(document.querySelector('[aria-current="true"]')).toBeNull()
    })
    it(`${view}: an addon's artifact links to its addon's page, never to a preview`, async () => {
      renderApp('/artifacts', { storage: { 'orch.artifacts.view.p_sev': view } })
      const link = await screen.findByRole('link', { name: /^Open addon page: .+ \(publish\)$/ }, T)
      expect(link).toHaveTextContent(/^Open addon page$/)
      expect(link.getAttribute('href')).toMatch(/\/addon\/publish\//)
      expect(screen.queryByRole('button', { name: /Preview share\/demo-0043-preview/ })).toBeNull()
    })
  }
  it('an addon without a page here gets a plain line, not a button', async () => {
    const setup = (s: MockStore) => {
      const ws = s.workspaces.find((w) => w.prefix === 'DEMO')!.id
      s.addonOp(ws, 'publish', { op: 'disable' }, { kind: 'person', id: 'p_sev' })
    }
    renderApp('/artifacts', { setup })
    await screen.findByText('12 artifacts', {}, T)
    await waitFor(() => expect(within(rowOf('share/demo-0043-preview')).getByText('Shown in the publish addon')).toBeInTheDocument(), T)
    expect(within(rowOf('share/demo-0043-preview')).queryByRole('button')).toBeNull()
  })
  it('a long addon title stays plain secondary text: the button keeps its core words', async () => {
    const real = api.getAddons
    const long = 'Apps, shares and a very long addon title!!'
    vi.spyOn(api, 'getAddons').mockImplementation(async () => (await real()).map((a) => (a.name === 'publish' ? { ...a, title: long } : a)))
    renderApp('/artifacts')
    const link = await screen.findByRole('link', { name: `Open addon page: ${long} (publish)` }, T)
    expect(link).toHaveTextContent(/^Open addon page$/)
    const who = within(rowOf('share/demo-0043-preview')).getByText(`${long} (publish)`)
    expect(who).toHaveClass('truncate')
    expect(who).toHaveAttribute('title', `${long} (publish)`)
  })
  it('the ticket key is a link to the ticket and does not preview', async () => {
    const { user } = renderApp('/artifacts')
    await screen.findByText('12 artifacts', {}, T)
    await user.click(within(rowOf('tariff-export.log')).getByRole('link', { name: 'DEMO-0043' }))
    expect(await screen.findByRole('heading', { level: 1, name: /Load tariff/ }, T)).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).toBeNull()
  })
  it('the drawer closes when its artifact leaves the results, and focus falls back to the results heading', async () => {
    const real = api.listArtifacts
    let hide = false
    vi.spyOn(api, 'listArtifacts').mockImplementation(async (ws, q) => {
      const r = await real(ws, q)
      return hide ? { ...r, items: r.items.filter((x) => x.name !== 'tariff-export.log') } : r
    })
    const { user, client } = renderApp('/artifacts')
    await user.click(await screen.findByRole('button', { name: 'Preview tariff-export.log' }, T))
    await screen.findByRole('dialog', {}, T)
    hide = true
    await act(() => client.invalidateQueries({ queryKey: ['artifacts'] }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), T)
    await waitFor(() => expect(document.getElementById('artifact-results')).toHaveFocus(), T)
    expect(document.getElementById('artifact-results')).toHaveClass('focus:not-sr-only')
  })
  it('Enter and Space on Preview open it', async () => {
    const { user } = renderApp('/artifacts')
    const preview = await screen.findByRole('button', { name: 'Preview tariff-export.log' }, T)
    preview.focus()
    await user.keyboard('{Enter}')
    expect(await screen.findByRole('dialog', {}, T)).toBeInTheDocument()
    await user.keyboard('{Escape}')
    await waitFor(() => expect(preview).toHaveFocus(), T)
    await user.keyboard(' ')
    expect(await screen.findByRole('dialog', {}, T)).toBeInTheDocument()
  })
  it('runs an HTML report in the sandboxed frame only while agent HTML is on', async () => {
    const { user } = renderApp('/artifacts')
    await user.click(await screen.findByRole('button', { name: 'Preview reconciliation-demo.html' }, T))
    const sheet = await screen.findByRole('dialog', {}, T)
    expect(await within(sheet).findByTitle(/Sandboxed preview of reconciliation-demo.html/, {}, T)).toHaveAttribute('sandbox', 'allow-scripts')
  })
  it('while new results load, the old ones are inert and the count says "Updating…"', async () => {
    const real = api.listArtifacts
    vi.spyOn(api, 'listArtifacts').mockImplementation((ws, q) => (q?.q ? new Promise(() => {}) : real(ws, q)))
    const { user } = renderApp('/artifacts')
    await screen.findByText('12 artifacts', {}, T)
    await user.type(screen.getByRole('searchbox', { name: 'Search artifacts' }), 'tolerance')
    expect(await screen.findByText('Updating…', {}, T)).toBeInTheDocument()
    const results = screen.getByRole('button', { name: 'Preview tariff-export.log' }).closest('[inert]')
    expect(results).not.toBeNull()
  })
})

describe('Artifacts page: viewer states', () => {
  it('an artifact that is no longer on its ticket says so instead of loading forever', async () => {
    const real = api.getTicket
    vi.spyOn(api, 'getTicket').mockImplementation(async (key) => ({ ...(await real(key)), artifacts: [] }))
    const { user } = renderApp('/artifacts')
    await user.click(await screen.findByRole('button', { name: 'Preview tariff-export.log' }, T))
    const sheet = await screen.findByRole('dialog', {}, T)
    expect(await within(sheet).findByText('This artifact is no longer on the ticket.', {}, T)).toBeInTheDocument()
    expect(within(sheet).queryByLabelText('Loading the artifact')).toBeNull()
  })
  it('a ticket the viewer may no longer see shows nothing about the artifact', async () => {
    vi.spyOn(api, 'getTicket').mockRejectedValue(new ApiError(404, { code: 'not_visible', message: 'Not visible', retryable: false }))
    const { user } = renderApp('/artifacts')
    await user.click(await screen.findByRole('button', { name: 'Preview tariff-export.log' }, T))
    const sheet = await screen.findByRole('dialog', {}, T)
    expect(await within(sheet).findByText('Artifact not available', {}, T)).toBeInTheDocument()
    expect(sheet).not.toHaveTextContent('tariff-export.log')
    expect(sheet).not.toHaveTextContent(/sha256/)
    expect(within(sheet).queryByRole('link')).toBeNull()
  })
  it('a failed load offers Try again', async () => {
    const real = api.getTicket
    const spy = vi.spyOn(api, 'getTicket').mockRejectedValueOnce(new ApiError(500, { code: 'internal', message: 'Boom', retryable: true }))
    spy.mockImplementation(real)
    const { user } = renderApp('/artifacts')
    await user.click(await screen.findByRole('button', { name: 'Preview tariff-export.log' }, T))
    const sheet = await screen.findByRole('dialog', {}, T)
    await user.click(await within(sheet).findByRole('button', { name: 'Try again' }, T))
    expect(await within(sheet).findByRole('button', { name: /Copy all/ }, T)).toBeInTheDocument()
  })
})

describe('artifact keys', () => {
  it('parseArtifactKey reads back an artifactKey (names may hold "/")', () => {
    expect(parseArtifactKey(artifactKey({ ticket: 'DEMO-1', name: 'share/x.md', sha256: 'abc' }))).toEqual({ ticket: 'DEMO-1', name: 'share/x.md', sha256: 'abc' })
    expect(parseArtifactKey('nonsense')).toBeNull()
  })
  it('the address id is the ticket key and the first 12 hash characters', () => {
    expect(artifactUrlId({ ticket: 'DEMO-0043', sha256: '9b1e44c07ad2ffff' })).toBe('DEMO-0043.9b1e44c07ad2')
  })
  it('focus falls back to the results heading when the item is gone', () => {
    const h = document.createElement('h2')
    h.id = 'artifact-results'
    document.body.append(h)
    try {
      expect(previewTarget('DEMO-1/gone/x')).toBe(h)
    } finally {
      h.remove()
    }
  })
})
