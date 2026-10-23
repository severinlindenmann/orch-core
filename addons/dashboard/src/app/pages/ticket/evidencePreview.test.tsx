// #1069: the evidence previewer's ladder, rung by rung. Every preview comes from bytes whose digest is the ticket's.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type { ArtifactContent } from '@/api/artifactText'
import { sha256HexBytes } from '@/api/sha256'
import type { Artifact } from '@/api/types'
import { Viewer } from './Artifacts'

const enc = (s: string) => new TextEncoder().encode(s)
const b64 = (b: Uint8Array) => btoa(String.fromCharCode(...b))
const PNG = Uint8Array.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 0])
const JPEG = Uint8Array.from([0xff, 0xd8, 0xff, 0xe0, 0, 0, 0, 0])
const SVG = enc('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script><rect width="1" height="1"/></svg>')

function show(name: string, over: Partial<ArtifactContent> & { bytes?: Uint8Array; sha?: string } = {}) {
  const bytes = over.bytes ?? enc('x')
  const digest = 'sha256:' + sha256HexBytes(bytes)
  const a = { name, kind: 'other', bytes: bytes.length, sha256: over.sha ?? digest, added_by: 'agent', at: '2026-10-23T10:00:00Z' } as Artifact
  const content: ArtifactContent = { name, size: bytes.length, digest, verified: true, truncated: false, binary: false, text: null, ...over }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  client.setQueryData(['workspaces'], [{ id: 'w1', prefix: 'DEMO', name: 'Demo', members: [], gates: {}, addons: {}, counts: {}, needs_you: 0 }])
  client.setQueryData(['artifact-text', 'w1', 'DEMO-1', name, a.sha256], content)
  return render(
    <QueryClientProvider client={client}>
      <Viewer a={a} agentHtml={false} ticket="DEMO-1" />
    </QueryClientProvider>,
  )
}
const media = (type: 'png' | 'jpeg' | 'svg', bytes: Uint8Array, digest = 'sha256:' + sha256HexBytes(bytes)) => ({ media: { type, b64: b64(bytes), digest } })

describe('pictures: an <img> from digest-checked bytes, never markup', () => {
  it('a PNG and a JPEG are drawn as <img> with a data: URL, scaled, click to enlarge', async () => {
    const user = userEvent.setup()
    show('a.png', { bytes: PNG, ...media('png', PNG) })
    const img = await screen.findByRole('img', { name: 'Picture a.png' })
    expect(img.getAttribute('src')).toMatch(/^data:image\/png;base64,/)
    expect(screen.getByText(/Scaled to fit/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Enlarge a.png' }))
    expect(screen.getByText(/Full size/)).toBeInTheDocument()
  })
  it('the enlarged picture keeps a maximum size (no max-w-none)', async () => {
    const user = userEvent.setup()
    show('a.png', { bytes: PNG, ...media('png', PNG) })
    await user.click(await screen.findByRole('button', { name: 'Enlarge a.png' }))
    const img = screen.getByRole('img', { name: 'Picture a.png' })
    expect(img.className).not.toMatch(/max-w-none/)
    expect(img.className).toMatch(/max-w-\[2000px\]/)
  })
  it('a JPEG is drawn too', async () => {
    show('b.jpg', { bytes: JPEG, ...media('jpeg', JPEG) })
    expect((await screen.findByRole('img', { name: 'Picture b.jpg' })).getAttribute('src')).toMatch(/^data:image\/jpeg;base64,/)
  })
  it('an SVG is an <img> only: no script, no svg element, no object or iframe in the page', async () => {
    const { container } = show('c.svg', { bytes: SVG, text: new TextDecoder().decode(SVG), ...media('svg', SVG) })
    const img = await screen.findByRole('img', { name: 'Picture c.svg' })
    expect(img.tagName).toBe('IMG')
    expect(img.getAttribute('src')).toMatch(/^data:image\/svg\+xml;base64,/)
    expect(container.querySelector('svg, script, object, iframe, embed')).toBeNull()
  })
  it('bytes that are not the ticket\'s are never drawn: a mismatch warning instead', async () => {
    show('d.png', { bytes: PNG, ...media('png', PNG, 'sha256:' + 'e'.repeat(64)) })
    expect(await screen.findByText(/picture does not match the digest/)).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: /Picture/ })).toBeNull()
  })
  it('a type that the first bytes contradict is not drawn', async () => {
    show('e.png', { bytes: JPEG, ...media('png', JPEG) })
    expect(await screen.findByText(/picture does not match the digest/)).toBeInTheDocument()
  })
})

describe('text-like files', () => {
  it('JSON is pretty-printed', async () => {
    show('r.json', { text: '{"a":1,"b":[2,3]}' })
    const pre = await screen.findByLabelText('JSON of r.json')
    expect(pre.textContent).toContain('{\n  "a": 1,')
  })
  it('a small CSV is a table', async () => {
    show('t.csv', { text: 'name,kwh\nA,1\nB,2\n' })
    const table = await screen.findByRole('table')
    expect(within(table).getByText('kwh')).toBeInTheDocument()
    expect(within(table).getAllByRole('row')).toHaveLength(3)
  })
  it('markdown is drawn with basic formatting, scripts and raw HTML inert', async () => {
    const { container } = show('n.md', { text: '# Title\n\nsome **bold** <script>alert(1)</script> [x](javascript:alert(1))' })
    expect(await screen.findByRole('heading', { name: 'Title' })).toBeInTheDocument()
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('a[href^="javascript"]')).toBeNull()
  })
  it('a link in agent markdown is plain text with its address, never an anchor', async () => {
    const { container } = show('n.md', { text: 'Please [Approve here](https://evil.example/x) now' })
    expect(await screen.findByText(/Approve here/)).toBeInTheDocument()
    expect(container.querySelector('a')).toBeNull()
    expect(container).toHaveTextContent('Approve here (https://evil.example/x)')
  })
  it('a diff is coloured by line', async () => {
    show('c.diff', { text: '--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n' })
    const pre = await screen.findByLabelText('Diff c.diff')
    expect(pre.querySelector('[data-diff="add"]')?.textContent).toBe('+new')
    expect(pre.querySelector('[data-diff="del"]')?.textContent).toBe('-old')
    expect(pre.querySelector('[data-diff="hunk"]')).not.toBeNull()
  })
  it('yaml, xml and any other UTF-8 file are shown as plain text', async () => {
    show('c.yaml', { text: 'a: 1\n' })
    expect(await screen.findByLabelText('Text of c.yaml')).toHaveTextContent('a: 1')
  })
})

describe('when there is no preview: a card that says why', () => {
  it('binary file: name, kind, size, short digest, the reason', async () => {
    show('x.bin', { bytes: Uint8Array.from([0, 1, 2]), binary: true })
    const card = await screen.findByTestId('no-preview-card')
    expect(card).toHaveTextContent('x.bin')
    expect(card).toHaveTextContent(/Binary file \(type \.bin\)/)
    expect(card).toHaveTextContent(/sha256 [0-9a-f]{12}/)
    expect(within(card).getByRole('button', { name: 'Copy file name' })).toBeInTheDocument()
  })
  it('PDF is not previewed (its viewer would run in the page) and says so', async () => {
    show('r.pdf', { bytes: enc('%PDF-1.4\0'), binary: true })
    expect(await screen.findByText(/PDF documents are not previewed here/)).toBeInTheDocument()
    expect(document.querySelector('iframe, object, embed')).toBeNull()
  })
  it('too large: says the size and the limit', async () => {
    show('big.log', { truncated: true, verified: false, size: 9 * 1024 * 1024, text: 'partial' })
    expect(await screen.findByText(/Too large to preview: .*the limit is/)).toBeInTheDocument()
  })
  it('a digest that differs from the ticket is a warning, never a preview', async () => {
    show('m.txt', { text: 'hello', sha: 'f'.repeat(64) })
    const card = await screen.findByTestId('no-preview-card')
    expect(card).toHaveAttribute('role', 'alert')
    expect(card).toHaveTextContent(/does not match the digest/)
    expect(screen.queryByLabelText('Text of m.txt')).toBeNull()
  })
})

describe('one SVG rule on page and server', () => {
  it('is an svg only when it starts with <svg, or <?xml / <!doctype svg with an <svg tag in the first 4 KiB', async () => {
    const { sniffImage } = await import('@/api/pasteImage')
    expect(sniffImage(enc('  <SVG xmlns="x"/>'))).toBe('svg')
    expect(sniffImage(enc('<?xml version="1.0"?><svg/>'))).toBe('svg')
    expect(sniffImage(enc('<!DOCTYPE svg><svg/>'))).toBe('svg')
    expect(sniffImage(enc('<?xml version="1.0"?><note>hi</note>'))).toBeNull()
    expect(sniffImage(enc('hello <svg/> inside text'))).toBeNull()
  })
})

describe('an SVG or GIF is held by its server-read type, not its name', () => {
  it('an SVG named shot.png under a criterion opens by name but draws nothing until a click', async () => {
    const user = userEvent.setup()
    const { CriterionEvidence } = await import('./EvidenceView')
    const bytes = SVG
    const sha = sha256HexBytes(bytes)
    const a = { name: 'shot.png', kind: 'screenshot', bytes: bytes.length, sha256: sha, added_by: 'agent', at: '2026-10-23T10:00:00Z', ac: 'AC1' } as Artifact
    const ticket = { key: 'DEMO-1', artifacts: [a], acceptance_state: [{ id: 'AC1', text: 'x', state: 'proven', evidence: [{ kind: 'artifact', ref: 'shot.png' }] }], tasks_state: [], gates: {} } as never
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    client.setQueryData(['workspaces'], [{ id: 'w1', prefix: 'DEMO', name: 'Demo', members: [], gates: {}, addons: {}, counts: {}, needs_you: 0 }])
    client.setQueryData(['artifact-text', 'w1', 'DEMO-1', 'shot.png', sha], { name: 'shot.png', size: bytes.length, digest: 'sha256:' + sha, verified: true, truncated: false, binary: false, text: null, ...media('svg', bytes) })
    render(
      <QueryClientProvider client={client}>
        <CriterionEvidence ticket={ticket} ac={(ticket as { acceptance_state: never[] }).acceptance_state[0]} />
      </QueryClientProvider>,
    )
    const hold = await screen.findByRole('button', { name: 'Show SVG picture' })
    expect(screen.queryByRole('img', { name: /Picture/ })).toBeNull()
    await user.click(hold)
    expect((await screen.findByRole('img', { name: 'Picture shot.png' })).getAttribute('src')).toMatch(/^data:image\/svg\+xml/)
  })
})
