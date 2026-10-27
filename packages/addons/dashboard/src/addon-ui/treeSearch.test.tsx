// `tree` and `search` (design D7): named reads, lazy children, keyboard navigation, selected state, loading / empty /
// error states, a limit on long lists, and the closed node schemas.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/types'
import { WorkspaceProvider } from '@/app/workspace'
import { AddonNode } from './AddonNode'
import { parseNode } from './nodes'
import { withoutTitle } from './TreeNode'
import { resetSelections } from './viewSelection'

type Handler = (name: string, query: URLSearchParams) => unknown
let handler: Handler = () => {
  throw new Error('no handler')
}
const calls: string[] = []
vi.mock('@/api/runs', async (orig) => ({
  ...(await orig<typeof import('@/api/runs')>()),
  call: async (_method: string, path: string) => {
    calls.push(path)
    const url = new URL(path, 'http://x')
    const name = url.pathname.split('/view/')[1]
    return handler(name, url.searchParams)
  },
}))

const TREE = { type: 'tree', id: 'pages', label: 'Wiki pages', read: 'wiki.tree', empty: 'No pages yet.', detail: { read: 'wiki.page', title: 'title', text: 'markdown', empty: 'Too large.' } }
const SEARCH = { type: 'search', id: 'find', label: 'Search the wiki', read: 'wiki.search', target: 'pages', limit: 2 }
const entry = (id: string, extra: object = {}) => ({ id, label: `Page ${id}`, ...extra })
const tree = (nodes: object[], extra: object = {}) => ({ state: 'ok', message: null, nodes, total: nodes.length, cut: false, ...extra })
const page = (id: string, markdown: string | null = `# Heading ${id}\n\nBody of ${id}.`) => ({ id, title: `Page ${id}`, markdown, updated: null, by: null })

function show(node: unknown, addon = 'wiki') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <WorkspaceProvider>
        <AddonNode node={node} addon={addon} />
      </WorkspaceProvider>
    </QueryClientProvider>,
  )
}
const stack = (...children: unknown[]) => ({ type: 'stack', children })

beforeEach(() => {
  calls.length = 0
  resetSelections()
  handler = () => {
    throw new Error('no handler')
  }
})
afterEach(cleanup)

describe('tree node schema', () => {
  it('accepts a named read and refuses a URL or a foreign shape', () => {
    expect(parseNode(TREE).ok).toBe(true)
    expect(parseNode({ ...TREE, read: 'https://evil.example/x' }).ok).toBe(false)
    expect(parseNode({ ...TREE, read: '/api/w/x/wiki' }).ok).toBe(false)
    expect(parseNode({ ...TREE, extra: 1 }).ok).toBe(false)
    expect(parseNode({ ...TREE, detail: { ...TREE.detail, read: 'wiki' } }).ok).toBe(false)
    expect(parseNode({ ...SEARCH, limit: 500 }).ok).toBe(false)
    expect(parseNode({ ...SEARCH, target: 'a b' }).ok).toBe(false)
  })
  it('refuses a read that is not in the add-on namespace without calling the server', async () => {
    show({ ...TREE, read: 'other.tree' })
    await new Promise((r) => setTimeout(r, 30))
    expect(calls).toEqual([])
    expect(screen.getByRole('status', { name: 'Loading' })).toBeInTheDocument()
  })
})

describe('tree', () => {
  it('shows a loading state, then the items, with the first one open beside the tree', async () => {
    vi.stubGlobal('matchMedia', () => ({ matches: true }))
    handler = (name, q) => (name === 'wiki.tree' ? tree([entry('a'), entry('b', { badge: '2 linked tickets' })]) : page(q.get('id') as string))
    show(TREE)
    expect(screen.getByRole('status', { name: 'Loading' })).toBeInTheDocument()
    const items = await screen.findAllByRole('treeitem')
    expect(items.map((i) => i.textContent)).toEqual(['Page a', 'Page b2 linked tickets'])
    expect(await screen.findByRole('heading', { name: 'Page a' })).toBeInTheDocument()
    expect(await screen.findByText('Body of a.')).toBeInTheDocument()
    expect(items[0]).toHaveAttribute('aria-selected', 'true')
    expect(items[1]).toHaveAttribute('aria-selected', 'false')
    vi.unstubAllGlobals()
  })

  it('opens an item on click and marks it selected; the pane follows', async () => {
    vi.stubGlobal('matchMedia', () => ({ matches: false }))
    handler = (name, q) => (name === 'wiki.tree' ? tree([entry('a'), entry('b')]) : page(q.get('id') as string))
    show(TREE)
    const user = userEvent.setup()
    expect(await screen.findByText('Choose an item to read it.')).toBeInTheDocument()
    await user.click(await screen.findByRole('treeitem', { name: /Page b/ }))
    expect(await screen.findByText('Body of b.')).toBeInTheDocument()
    expect(screen.getByRole('treeitem', { name: /Page b/ })).toHaveAttribute('aria-selected', 'true')
    expect(calls.some((c) => c.includes('/view/wiki.page?id=b'))).toBe(true)
    // on a phone the tree gives way to the page, with a way back
    await user.click(screen.getByRole('button', { name: /All items/ }))
    expect(await screen.findByText('Choose an item to read it.')).toBeInTheDocument()
    vi.unstubAllGlobals()
  })

  it('moves with the arrow keys and opens with Enter (roving tabindex)', async () => {
    vi.stubGlobal('matchMedia', () => ({ matches: false }))
    handler = (name, q) => (name === 'wiki.tree' ? tree([entry('a'), entry('b'), entry('c')]) : page(q.get('id') as string))
    show(TREE)
    const user = userEvent.setup()
    const [a, b, c] = await screen.findAllByRole('treeitem')
    expect(a).toHaveAttribute('tabindex', '0')
    expect(b).toHaveAttribute('tabindex', '-1')
    a.focus()
    await user.keyboard('{ArrowDown}')
    expect(b).toHaveFocus()
    expect(b).toHaveAttribute('tabindex', '0')
    await user.keyboard('{End}')
    expect(c).toHaveFocus()
    await user.keyboard('{Home}{ArrowDown}{Enter}')
    expect(await screen.findByText('Body of b.')).toBeInTheDocument()
    expect(screen.getByRole('treeitem', { name: /Page b/ })).toHaveAttribute('aria-selected', 'true')
    vi.unstubAllGlobals()
  })

  it('loads children lazily: ArrowRight opens, ArrowLeft closes or goes to the parent', async () => {
    vi.stubGlobal('matchMedia', () => ({ matches: false }))
    handler = (name, q) => {
      if (name === 'wiki.tree') return q.get('parent') === 'dir' ? tree([entry('kid')]) : tree([entry('dir', { children: true }), entry('z')])
      return page(q.get('id') as string)
    }
    show(TREE)
    const user = userEvent.setup()
    const dir = await screen.findByRole('treeitem', { name: /Page dir/ })
    expect(dir).toHaveAttribute('aria-expanded', 'false')
    expect(calls.some((c) => c.includes('parent=dir'))).toBe(false)
    dir.focus()
    await user.keyboard('{ArrowRight}')
    const kid = await screen.findByRole('treeitem', { name: /Page kid/ })
    expect(dir).toHaveAttribute('aria-expanded', 'true')
    expect(kid).toHaveAttribute('aria-level', '2')
    await user.keyboard('{ArrowRight}')
    expect(kid).toHaveFocus()
    await user.keyboard('{ArrowLeft}')
    expect(dir).toHaveFocus()
    await user.keyboard('{ArrowLeft}')
    await waitFor(() => expect(screen.queryByRole('treeitem', { name: /Page kid/ })).not.toBeInTheDocument())
    expect(dir).toHaveAttribute('aria-expanded', 'false')
    vi.unstubAllGlobals()
  })

  it('says so when there is nothing, with the read\'s own message and offer', async () => {
    handler = () => tree([], { state: 'no_folder', message: 'No wiki folder in this project yet.', offer: { label: 'Create the wiki folder', action: 'create' } })
    show(TREE)
    expect(await screen.findByText('No wiki folder in this project yet.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Create the wiki folder' })).toBeInTheDocument()
    expect(screen.queryByText('Choose an item to read it.')).not.toBeInTheDocument() // no empty pane beside an empty list
  })

  it('falls back to the node\'s empty text', async () => {
    handler = () => tree([])
    show(TREE)
    expect(await screen.findByText('No pages yet.')).toBeInTheDocument()
  })

  it('shows an error with Retry, and recovers', async () => {
    let fail = true
    handler = () => {
      if (fail) throw new ApiError(500, { code: 'host.unavailable', message: 'x', retryable: true })
      return tree([entry('a')])
    }
    show(TREE)
    const user = userEvent.setup()
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load wiki pages')
    fail = false
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('treeitem', { name: /Page a/ })).toBeInTheDocument()
  })

  it('rejects a malformed answer instead of drawing it', async () => {
    handler = () => ({ nodes: 'nope' })
    show(TREE)
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load')
  })

  it('draws a long list in pages with "Show more", and says when the read cut it', async () => {
    const many = Array.from({ length: 230 }, (_, i) => entry(`p${String(i).padStart(3, '0')}`))
    handler = () => tree(many, { total: 300, cut: true })
    show({ ...TREE, detail: undefined })
    const user = userEvent.setup()
    expect(await screen.findAllByRole('treeitem')).toHaveLength(100)
    expect(screen.getByText('Showing the first 230 of 300.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /Show more \(130 left\)/ }))
    expect(screen.getAllByRole('treeitem')).toHaveLength(200)
    await user.click(screen.getByRole('button', { name: /Show more \(30 left\)/ }))
    expect(screen.getAllByRole('treeitem')).toHaveLength(230)
    expect(screen.queryByRole('button', { name: /Show more/ })).not.toBeInTheDocument()
  })

  it('shows the page states: loading, too large, gone, failed', async () => {
    vi.stubGlobal('matchMedia', () => ({ matches: false }))
    handler = (name, q) => {
      if (name === 'wiki.tree') return tree([entry('big'), entry('gone'), entry('bad')])
      const id = q.get('id')
      if (id === 'big') return page('big', null)
      if (id === 'gone') throw new ApiError(404, { code: 'not_found', message: 'x', retryable: false })
      throw new ApiError(500, { code: 'host.unavailable', message: 'x', retryable: true })
    }
    show(TREE)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('treeitem', { name: /Page big/ }))
    expect(await screen.findByText('Too large.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /All items/ }))
    await user.click(await screen.findByRole('treeitem', { name: /Page gone/ }))
    expect(await screen.findByText('This item is no longer there.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Back to the list' }))
    await user.click(await screen.findByRole('treeitem', { name: /Page bad/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load this item')
    vi.unstubAllGlobals()
  })

  it('draws page text as safe markdown (no raw HTML)', async () => {
    vi.stubGlobal('matchMedia', () => ({ matches: true }))
    handler = (name) => (name === 'wiki.tree' ? tree([entry('a')]) : { ...page('a'), markdown: '## Sub\n\n<script>window.hacked=1</script><img src=x onerror="window.hacked=1">\n\n[ok](https://example.com)' })
    const { container } = show(TREE)
    expect(await screen.findByRole('heading', { name: 'Sub' })).toBeInTheDocument()
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('img[onerror]')).toBeNull()
    vi.unstubAllGlobals()
  })
})

describe('page header', () => {
  it('does not repeat a first "# title" line, and keeps any other text', () => {
    expect(withoutTitle('# Runbook\n\nBody', 'Runbook')).toBe('\nBody')
    expect(withoutTitle('\n# Runbook\r\nBody', 'Runbook')).toBe('Body')
    expect(withoutTitle('# Runbook', 'Runbook')).toBe('')
    expect(withoutTitle('# Other\nBody', 'Runbook')).toBe('# Other\nBody')
    expect(withoutTitle('## Runbook\nBody', 'Runbook')).toBe('## Runbook\nBody')
  })
})

describe('search', () => {
  it('waits for typing to pause, lists hits, and opens a hit in the target tree', async () => {
    vi.stubGlobal('matchMedia', () => ({ matches: false }))
    handler = (name, q) => {
      if (name === 'wiki.tree') return tree([entry('a'), entry('b')])
      if (name === 'wiki.search') return { results: [{ id: 'b', label: 'Page b', snippet: 'a needle in b' }], total: 1, cut: false }
      return page(q.get('id') as string)
    }
    show(stack(SEARCH, TREE))
    const user = userEvent.setup()
    await user.type(await screen.findByRole('searchbox', { name: 'Search the wiki' }), 'needle')
    const hit = await screen.findByRole('button', { name: /Page b/ })
    expect(calls.filter((c) => c.includes('wiki.search'))).toEqual(['/api/w/' + calls[0].split('/')[3] + '/view/wiki.search?q=needle'])
    expect(within(hit).getByText('a needle in b')).toBeInTheDocument()
    await user.click(hit)
    expect(await screen.findByText('Body of b.')).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Search results' })).not.toBeInTheDocument()
    expect(screen.getByRole('searchbox')).toHaveValue('')
    vi.unstubAllGlobals()
  })

  it('says when nothing matches, and when searching fails', async () => {
    let mode: 'none' | 'fail' = 'none'
    handler = () => {
      if (mode === 'fail') throw new ApiError(500, { code: 'host.unavailable', message: 'x', retryable: true })
      return { results: [], total: 0, cut: false }
    }
    show(SEARCH)
    const user = userEvent.setup()
    const box = screen.getByRole('searchbox')
    await user.type(box, 'zzz')
    expect(await screen.findByText(/Nothing matches/)).toHaveTextContent('Nothing matches “zzz”.')
    mode = 'fail'
    await user.clear(box)
    await user.type(box, 'qqq')
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load the search')
  })

  it('draws at most `limit` hits and says how many more', async () => {
    handler = () => ({ results: [{ id: 'a', label: 'A' }, { id: 'b', label: 'B' }, { id: 'c', label: 'C' }, { id: 'd', label: 'D' }], total: 4, cut: true })
    show(SEARCH)
    await userEvent.setup().type(screen.getByRole('searchbox'), 'x')
    expect(await screen.findAllByRole('listitem')).toHaveLength(2)
    expect(screen.getByText(/2 more matches/)).toHaveTextContent('The search stopped early')
  })

  it('clears with Escape and sends nothing for blank text', async () => {
    handler = () => ({ results: [{ id: 'a', label: 'A' }], total: 1, cut: false })
    show(SEARCH)
    const user = userEvent.setup()
    const box = screen.getByRole('searchbox')
    await user.type(box, '   ')
    await new Promise((r) => setTimeout(r, 350))
    expect(calls).toEqual([])
    await user.type(box, 'a')
    await screen.findByRole('list', { name: 'Search results' })
    await user.keyboard('{Escape}')
    expect(box).toHaveValue('')
    expect(screen.queryByRole('list', { name: 'Search results' })).not.toBeInTheDocument()
  })
})
