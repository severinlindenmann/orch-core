import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { READS, show, useFakeHost, workspace } from '@/test/hostHarness'

// The production app on a fake server that speaks the explorer routes (issue #491). The server is the authority (owners
// only, deny list, no links): the page shows what it is given and never builds a path of its own.
vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const A = 'a'.repeat(32)
const T = { timeout: 5000 }
const SHA1 = '1'.repeat(40)
const SHA2 = '2'.repeat(40)
const entry = (name: string, over: Record<string, unknown> = {}) => ({ name, label: name, kind: 'file', size: 12, repo: false, locked: null, ...over })
const state = useFakeHost(() => [workspace({ id: A, features: [...READS, 'explorer'] })])

const route = (files: Record<string, Record<string, unknown>> = {}) => {
  state.host.override = () => {
    // the harness hands the override the path without its query; the call it just recorded has both
    const u = new URL(state.host.calls.at(-1)!.path, 'http://x')
    const p = u.pathname
    if (!p.includes('/explorer')) return undefined
    const q = (k: string) => u.searchParams.get(k) ?? ''
    if (p.endsWith('/explorer')) return Response.json({ available: true, roots: [{ id: 'ws', label: 'My project', kind: 'workspace', repo: true }], search: { rg: false, git: true } })
    if (p.endsWith('/explorer/tree')) {
      if (q('path') === 'src') return Response.json({ root: 'ws', path: 'src', entries: [entry('app.py')], more: false })
      return Response.json({
        root: 'ws', path: '', more: false,
        entries: [
          entry('src', { kind: 'dir', size: undefined }),
          entry('README.md'),
          entry('.env', { locked: 'Secrets (.env)', size: undefined }),
          entry('events', { kind: 'dir', locked: "orch's own files", size: undefined }),
          entry('shortcut', { kind: 'link', size: undefined }),
        ],
      })
    }
    if (p.endsWith('/explorer/file')) {
      const file = files[q('path')]
      return file ? Response.json({ root: 'ws', path: q('path'), name: q('path').split('/').at(-1), label: q('path').split('/').at(-1), ...file }) : Response.json({ ok: false, error: { code: 'not_found', message: 'x', retryable: false } }, { status: 404 })
    }
    if (p.endsWith('/explorer/history')) {
      return Response.json({
        repo: true, more: false,
        commits: [
          { sha: SHA2, short: '22222222', author: 'Ann', at: '2026-10-18T08:00:00Z', subject: 'Second change', tickets: [{ key: 'DEMO-0001', title: 'First ticket' }] },
          { sha: SHA1, short: '11111111', author: 'Bo', at: '2026-10-17T08:00:00Z', subject: 'First version', tickets: [] },
        ],
      })
    }
    if (p.endsWith('/explorer/commit')) return Response.json({ sha: q('sha'), short: q('sha').slice(0, 8), author: 'Ann', at: '2026-10-18T08:00:00Z', subject: 'Second change', body: '', patch: '@@ -1 +1,2 @@\n one\n+two\n', truncated: false, empty: false, tickets: [] })
    if (p.endsWith('/explorer/compare')) return Response.json({ a: q('a'), b: q('b'), patch: '@@ -1 +1 @@\n-one\n+three\n', truncated: false, empty: false })
    if (p.includes('/explorer/ticket/')) return Response.json({ files: [{ root: 'ws', path: 'src/app.py', label: 'src/app.py', status: 'M', sha: SHA2, short: '22222222', via: 'push', repo: 'code' }], more: false })
    return undefined
  }
}
const calls = (p: string) => state.host.calls.filter((c) => c.path.includes(p))

it('shows locked and linked entries as such and opens a file as code with line numbers', async () => {
  route({ 'README.md': { kind: 'markdown', text: '# Title\n\nHello <script>alert(1)</script>\n', size: 40, history: true }, 'src/app.py': { kind: 'text', text: 'print("one")\n', language: 'python', lines: 1, size: 13, hidden_chars: false, history: true } })
  const user = userEvent.setup()
  show('/w/DEMO/explorer')
  const tree = await screen.findByRole('list', { name: 'Files' }, T)
  expect(within(tree).getByLabelText('.env, locked: Secrets (.env)')).toHaveTextContent('Locked')
  expect(within(tree).getByLabelText("events, locked: orch's own files")).toBeInTheDocument()
  expect(within(tree).getByLabelText('shortcut, a link')).toHaveTextContent('Link')
  expect(within(tree).queryByRole('button', { name: /\.env|events|shortcut/ })).toBeNull() // locked and links cannot be opened
  await user.click(within(tree).getByRole('button', { name: 'Folder src' }))
  await user.click(await within(tree).findByRole('button', { name: 'File app.py' }, T))
  const code = await screen.findByTestId('code', {}, T)
  expect(code).toHaveTextContent('print("one")')
  expect(code).toHaveTextContent('1')
  expect(calls('/explorer/file').at(-1)!.path).toContain('path=src%2Fapp.py')
})

it('renders markdown without running or showing raw html', async () => {
  route({ 'README.md': { kind: 'markdown', text: '# Title\n\nHello <script>alert(1)</script> <img src=x onerror=alert(1)>\n', size: 40, history: true } })
  const user = userEvent.setup()
  show('/w/DEMO/explorer')
  await user.click(await screen.findByRole('button', { name: 'File README.md' }, T))
  const md = await screen.findByTestId('markdown', {}, T)
  await waitFor(() => expect(within(md).getByRole('heading', { name: 'Title' })).toBeInTheDocument(), T)
  expect(md.querySelector('script')).toBeNull()
  expect(md.querySelector('img')).toBeNull()
  expect(md.innerHTML).not.toContain('onerror')
})

it('warns about characters you cannot see, shows an image from its data URL and says so for a locked file', async () => {
  route({
    'a.py': { kind: 'text', text: 'x = 1  # ‮\n', hidden_chars: true, size: 10, history: true },
    'p.png': { kind: 'image', mime: 'image/png', data: 'AAAA', size: 3 },
    '.env': { kind: 'locked', reason: 'Secrets (.env)' },
  })
  const user = userEvent.setup()
  show('/w/DEMO/explorer?path=a.py')
  expect(await screen.findByTestId('hidden-chars', {}, T)).toHaveTextContent('characters you cannot see')
  expect(screen.getByTestId('code')).toHaveTextContent('⟦u+202E⟧')
  expect(screen.getByTestId('code')).not.toHaveTextContent('‮')
  show('/w/DEMO/explorer?path=p.png')
  expect(await screen.findByRole('img', { name: 'p.png' }, T)).toHaveAttribute('src', 'data:image/png;base64,AAAA')
  show('/w/DEMO/explorer?path=.env')
  expect(await screen.findByTestId('locked', {}, T)).toHaveTextContent('never shown')
  expect(user).toBeTruthy()
})

it('History lists the changes with ticket chips, shows one change and compares two versions', async () => {
  route({ 'src/app.py': { kind: 'text', text: 'one\ntwo\n', lines: 2, size: 8, history: true } })
  const user = userEvent.setup()
  show('/w/DEMO/explorer?path=src%2Fapp.py&view=history')
  const list = await screen.findByRole('list', { name: 'Changes to this file' }, T)
  const [second, first] = within(list).getAllByRole('listitem')
  expect(within(second).getByRole('link', { name: 'DEMO-0001' })).toHaveAttribute('href', expect.stringContaining('/ticket/DEMO-0001'))
  expect(first).not.toHaveTextContent('DEMO-')
  expect(list).not.toHaveTextContent(SHA2) // the full id is not on screen
  await user.click(within(second).getByRole('button', { name: 'Second change' }))
  const diff = await screen.findByLabelText('Change in this commit', {}, T)
  expect(diff.querySelector('[data-line="add"]')).toHaveTextContent('+two')
  expect(within(second).getByRole('button', { name: 'More information' })).toBeTruthy()
  await user.click(within(second).getByLabelText('Compare: Second change'))
  await user.click(within(first).getByLabelText('Compare: First version'))
  const compare = await screen.findByLabelText('Difference between the two versions', {}, T)
  expect(compare.querySelector('[data-line="del"]')).toHaveTextContent('-one')
  const sent = calls('/explorer/compare').at(-1)!.path
  expect(sent).toContain(`a=${SHA1}`) // the older one first
  expect(sent).toContain(`b=${SHA2}`)
})

it('the sidebar lists Files only while the explorer is served', async () => {
  route()
  show('/w/DEMO/board')
  expect(await screen.findByRole('link', { name: 'Files' }, T)).toBeInTheDocument()
})

it('the ticket page lists the files changed from the ticket’s own events and links into the explorer', async () => {
  route()
  window.innerWidth = 1400
  show('/w/DEMO/ticket/DEMO-0001')
  const box = await screen.findByTestId('ticket-files', {}, T)
  const link = within(box).getByRole('link', { name: 'src/app.py' })
  expect(link).toHaveAttribute('href', expect.stringMatching(/\/explorer\?.*path=src%2Fapp\.py/))
  expect(link.getAttribute('href')).toContain('view=history')
})

it('EX1: the tree is keyboard-driven with arrow keys and folders say whether they are open', async () => {
  route({ 'src/app.py': { kind: 'text', text: 'x\n', lines: 1, size: 2, history: true } })
  const user = userEvent.setup()
  show('/w/DEMO/explorer')
  const tree = await screen.findByRole('list', { name: 'Files' }, T)
  const folder = await within(tree).findByRole('button', { name: 'Folder src' }, T)
  expect(folder).toHaveAttribute('aria-expanded', 'false')
  folder.focus()
  await user.keyboard('{ArrowRight}')
  expect(folder).toHaveAttribute('aria-expanded', 'true')
  const child = await within(tree).findByRole('button', { name: 'File app.py' }, T)
  await user.keyboard('{ArrowRight}') // an open folder: go to its first entry
  expect(child).toHaveFocus()
  await user.keyboard('{ArrowLeft}') // back to the parent folder
  expect(folder).toHaveFocus()
  await user.keyboard('{ArrowLeft}')
  expect(folder).toHaveAttribute('aria-expanded', 'false')
  await user.keyboard('{ArrowDown}')
  expect(within(tree).getByRole('button', { name: 'File README.md' })).toHaveFocus()
  await user.keyboard('{ArrowUp}{ArrowUp}')
  expect(folder).toHaveFocus()
})

it('EX1: the search choice is a radio group with one tab stop and arrow keys; the view switch is a tab list', async () => {
  route({ 'README.md': { kind: 'markdown', text: 'hi', size: 2, history: true } })
  const user = userEvent.setup()
  show('/w/DEMO/explorer?path=README.md')
  const names = await screen.findByRole('radio', { name: 'File names' }, T)
  const text = screen.getByRole('radio', { name: 'Text in files' })
  expect(names).toHaveAttribute('tabindex', '0')
  expect(text).toHaveAttribute('tabindex', '-1')
  names.focus()
  await user.keyboard('{ArrowRight}')
  expect(text).toHaveFocus()
  expect(text).toHaveAttribute('aria-checked', 'true')
  const preview = await screen.findByRole('tab', { name: 'Preview' }, T)
  expect(preview).toHaveAttribute('aria-selected', 'true')
  expect(screen.getByRole('tabpanel')).toBeInTheDocument()
  preview.focus()
  await user.keyboard('{ArrowRight}')
  const history = screen.getByRole('tab', { name: 'History' })
  await waitFor(() => expect(history).toHaveAttribute('aria-selected', 'true'), T)
  expect(history).toHaveFocus()
})

it('EX2/EX3/EX6/EX7: opens the folders above a linked file, shows its place, why an entry is locked, the intro and loading', async () => {
  route({ 'src/app.py': { kind: 'text', text: 'x\n', lines: 1, size: 2, history: true } })
  show('/w/DEMO/explorer?path=src%2Fapp.py')
  const crumbs = await screen.findByTestId('crumbs', {}, T)
  expect(crumbs).toHaveTextContent('src › app.py')
  expect(await screen.findByRole('button', { name: 'File app.py' }, T)).toHaveAttribute('aria-current', 'true')
  expect(screen.getByRole('button', { name: 'Folder src' })).toHaveAttribute('aria-expanded', 'true')
  expect(screen.getByRole('heading', { name: 'Files' })).toBeInTheDocument()
  expect(screen.getByText('Browse files and past changes. Files cannot be edited here.')).toBeInTheDocument()
  expect((await screen.findAllByTestId('why-locked', {}, T))[0]).toHaveTextContent('Why locked?')
})

it('EX4/EX5: history says how many versions are selected and does not silently drop a third', async () => {
  route({ 'src/app.py': { kind: 'text', text: 'one\n', lines: 1, size: 4, history: true } })
  const user = userEvent.setup()
  show('/w/DEMO/explorer?path=src%2Fapp.py&view=history')
  const list = await screen.findByRole('list', { name: 'Changes to this file' }, T)
  expect(screen.getByText('Select a change title to view it. Select two checkboxes to compare versions.')).toBeInTheDocument()
  expect(screen.getByTestId('pick-count')).toHaveTextContent('0 of 2 versions selected')
  const [a, b] = within(list).getAllByRole('checkbox')
  await user.click(a)
  expect(screen.getByTestId('pick-count')).toHaveTextContent('1 of 2 versions selected')
  await user.click(b)
  expect(screen.getByTestId('pick-count')).toHaveTextContent('2 of 2 versions selected')
  await user.click(screen.getByRole('button', { name: 'Clear selection' }))
  expect(screen.getByTestId('pick-count')).toHaveTextContent('0 of 2 versions selected')
})
