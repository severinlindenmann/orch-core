// A receipt or log with no inline content is read through the dashboard server and shown as escaped plain text.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { capText, isTextArtifact, looksBinary, TEXT_PREVIEW_BYTES, type ArtifactContent } from '@/api/artifactText'
import type { Artifact } from '@/api/types'
import { Viewer } from './Artifacts'

const art = (over: Partial<Artifact> = {}): Artifact => ({ name: 'T1.receipt.log', kind: 'receipt', bytes: 40, sha256: 'a'.repeat(64), added_by: 'orch', at: '2026-10-19T10:00:00Z', ...over }) as Artifact
const content = (over: Partial<ArtifactContent> = {}): ArtifactContent => ({ name: 'T1.receipt.log', size: 40, digest: 'sha256:' + 'a'.repeat(64), verified: true, truncated: false, binary: false, text: 'ok', ...over })

function show(a: Artifact, data?: ArtifactContent, ticket: string | null = 'DEMO-1') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['workspaces'], [{ id: 'w1', prefix: 'DEMO', name: 'Demo', members: [], gates: {}, addons: {}, counts: {}, needs_you: 0 }])
  if (data) client.setQueryData(['artifact-text', 'w1', 'DEMO-1', a.name, a.sha256], data)
  return render(
    <QueryClientProvider client={client}>
      <Viewer a={a} agentHtml={false} ticket={ticket ?? undefined} />
    </QueryClientProvider>,
  )
}

describe('which files are text', () => {
  it('names and kinds, never html, add-ons, links or files without bytes', () => {
    for (const name of ['a.log', 'a.txt', 'a.json', 'a.md', 'A.LOG']) expect(isTextArtifact({ name, kind: 'other', bytes: 3 } as Artifact)).toBe(true)
    expect(isTextArtifact(art({ name: 'x.bin', kind: 'log' }))).toBe(true)
    expect(isTextArtifact(art({ name: 'x.html', kind: 'report' }))).toBe(false)
    expect(isTextArtifact(art({ name: 'x.png', kind: 'screenshot' }))).toBe(false)
    expect(isTextArtifact(art({ addon: 'wiki' } as Partial<Artifact>))).toBe(false)
    expect(isTextArtifact(art({ bytes: null } as unknown as Partial<Artifact>))).toBe(false)
  })
  it('caps at 64 KiB without cutting a character, and spots NUL bytes', () => {
    expect(capText('abc')).toEqual({ shown: 'abc', cut: false, bytes: 3 })
    const text = 'é'.repeat(TEXT_PREVIEW_BYTES) // 2 bytes each
    const out = capText(text)
    expect(out.cut).toBe(true)
    expect(new TextEncoder().encode(out.shown).length).toBeLessThanOrEqual(TEXT_PREVIEW_BYTES)
    expect(out.shown).not.toContain('�')
    expect(looksBinary('ab\0cd')).toBe(true)
    expect(looksBinary('plain')).toBe(false)
  })
})

describe('text artifact preview', () => {
  it('shows the text as plain text, with control and bidi characters escaped, never as markup', async () => {
    show(art(), content({ text: 'line1\r\nESC\u001b[31m red ‮evil <b>x</b> <script>alert(1)</script>\n' }))
    const pre = await screen.findByLabelText('Text of T1.receipt.log')
    expect(pre.textContent).toContain('line1ESC\\u{1b}[31m red \\u{202e}evil <b>x</b> <script>alert(1)</script>')
    expect(pre.textContent).not.toContain('‮')
    expect(pre.querySelector('b, script')).toBeNull()
    expect(screen.getByText(/Shown as text. Nothing in the file is run/)).toBeInTheDocument()
    expect(screen.queryByText(/No inline preview/)).toBeNull()
  })
  it('says it shows the first 64 KiB of a bigger file', async () => {
    show(art(), content({ text: 'x'.repeat(TEXT_PREVIEW_BYTES + 500), size: TEXT_PREVIEW_BYTES + 500 }))
    expect(await screen.findByText(/Showing the first 64 KiB of/)).toBeInTheDocument()
    const pre = screen.getByLabelText('Text of T1.receipt.log')
    expect(pre.textContent!.length).toBe(TEXT_PREVIEW_BYTES)
  })
  it('a file the server calls binary, or with NUL bytes, or that cannot be read, stays "no preview"', async () => {
    const a = show(art(), content({ binary: true, text: null }))
    expect(await screen.findByText(/Binary file/)).toBeInTheDocument()
    a.unmount()
    const b = show(art(), content({ text: 'ab\0cd' }))
    expect(await screen.findByText(/Binary file/)).toBeInTheDocument()
    b.unmount()
    show(art({ name: 'x.png', kind: 'other' }))
    expect(await screen.findByText(/could not read this file/)).toBeInTheDocument()
  })
  it('a file whose digest differs from the listed one is not previewed', async () => {
    show(art(), content({ digest: 'sha256:' + 'b'.repeat(64) }))
    expect(await screen.findByText(/does not match the digest/)).toBeInTheDocument()
    expect(screen.queryByLabelText('Text of T1.receipt.log')).toBeNull()
  })
  it('a file read only in part (unverified) is too large to preview', async () => {
    show(art(), content({ truncated: true, verified: false, text: 'partial' }))
    expect(await screen.findByText(/Too large to preview/)).toBeInTheDocument()
    expect(screen.queryByLabelText('Text of T1.receipt.log')).toBeNull()
  })
  it('without the ticket there is nothing to read, so no preview', () => {
    show(art(), content(), null)
    expect(screen.getByText(/No inline preview/)).toBeInTheDocument()
  })
})
