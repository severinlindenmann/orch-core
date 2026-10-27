// The Wiki on a real host is a declarative view of the wiki add-on package, drawn through the nav slot: the package comes
// from GET /api/addons, its reads from GET /api/w/:ws/view/<name>. No page of the wiki is compiled into the app.
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { READS, show, useFakeHost, workspace } from '@/test/hostHarness'
import { WIKI_PREVIEW_NODE } from '@/dev/wikiPreview'

vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const A = 'a'.repeat(32)
const T = { timeout: 5000 }
const VIEW = JSON.parse(readFileSync(resolve(process.cwd(), '../wiki/views/wiki.json'), 'utf8'))
const PACKAGE = { name: 'wiki', title: 'Wiki', version: '0.1.0', description: 'Team notes.', capabilities: [], first_party: true, package_sha256: 'x', update: null, contributions: VIEW.contributions, actions: VIEW.actions }

let folder = true
const entry = (id: string, label: string) => ({ id, label, hint: id })
const state = useFakeHost(() => [workspace({ id: A, features: [...READS, 'actions', 'wiki', 'addons'] })])
const route = () => {
  folder = true
  state.host.override = (path, method) => {
    if (path === '/api/addons') return Response.json([PACKAGE])
    if (path.endsWith('/view/wiki.tree'))
      return Response.json(folder ? { state: 'ok', message: null, nodes: [entry('runbook', 'On-call runbook'), entry('glossary', 'Glossary')], total: 2, cut: false } : { state: 'no_folder', message: 'No wiki folder in this project yet.', nodes: [], total: 0, cut: false, offer: { label: 'Create the wiki folder', action: 'create' } })
    if (path.endsWith('/view/wiki.page')) {
      const id = new URLSearchParams(state.host.calls.at(-1)?.path.split('?')[1]).get('id')
      return Response.json({ id, title: id === 'runbook' ? 'On-call runbook' : 'Glossary', markdown: `# ${id}\n\nText of ${id}.`, updated: null, by: null })
    }
    if (path.endsWith('/view/wiki.search')) return Response.json({ results: [{ id: 'glossary', label: 'Glossary', snippet: 'a needle' }], total: 1, cut: false })
    if (path.endsWith('/wiki/create') && method === 'POST') {
      folder = true
      return Response.json({ created: true, path: 'wiki/readme.md' })
    }
    return undefined
  }
}

it('ships the same view as the layout preview and the package', () => {
  expect(VIEW.contributions[0].node).toEqual(WIKI_PREVIEW_NODE)
})

it('the sidebar lists Wiki from the add-on package, and its page reads pages through named reads', async () => {
  route()
  const user = userEvent.setup()
  show('/w/DEMO/addon/wiki/wiki')
  expect(await screen.findByRole('link', { name: /Wiki/ }, T)).toHaveAttribute('href', expect.stringContaining('/addon/wiki/wiki'))
  const tree = await screen.findByRole('tree', { name: 'Wiki pages' }, T)
  expect(within(tree).getAllByRole('treeitem').map((i) => i.textContent)).toEqual(['On-call runbook', 'Glossary'])
  await user.click(within(tree).getByRole('treeitem', { name: 'Glossary' }))
  expect(await screen.findByText('Text of glossary.', {}, T)).toBeInTheDocument()
  expect(state.host.calls.some((c) => c.path.includes('/view/wiki.page?id=glossary'))).toBe(true)
  // only reads: nothing was posted
  expect(state.host.calls.filter((c) => c.method === 'POST')).toEqual([])
})

it('search lists hits and opens one in the tree', async () => {
  route()
  const user = userEvent.setup()
  show('/w/DEMO/addon/wiki/wiki')
  await user.type(await screen.findByRole('searchbox', { name: 'Search the wiki' }, T), 'needle')
  await user.click(await screen.findByRole('button', { name: /Glossary/ }, T))
  expect(await screen.findByText('Text of glossary.', {}, T)).toBeInTheDocument()
})

it('an empty project offers to create the wiki folder, once, and then lists the pages', async () => {
  route()
  folder = false
  const user = userEvent.setup()
  show('/w/DEMO/addon/wiki/wiki')
  expect(await screen.findByText('No wiki folder in this project yet.', {}, T)).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Create the wiki folder' }))
  expect(await screen.findByRole('treeitem', { name: 'Glossary' }, T)).toBeInTheDocument()
  expect(state.host.calls.filter((c) => c.method === 'POST')).toHaveLength(1)
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Create the wiki folder' })).not.toBeInTheDocument())
})

it('with the add-on off Wiki is not listed and nothing is read', async () => {
  route()
  state.host.workspaces[0].features = READS
  show('/w/DEMO/board')
  await screen.findByRole('heading', { level: 1 }, T)
  expect(screen.queryByRole('link', { name: /^Wiki/ })).toBeNull()
  expect(state.host.calls.some((c) => c.path.includes('/view/'))).toBe(false)
})
