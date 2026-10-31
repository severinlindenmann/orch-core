import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'
import type { ArtifactContent } from '@/api/artifactText'
import type { Artifact } from '@/api/types'
import { EvidenceThumb, typeBadge, cardTitle, checkOf } from './EvidenceThumb'

const SHA = 'a'.repeat(64)
const art = (name: string, over: Partial<Artifact> = {}): Artifact => ({ name, kind: 'other', bytes: 100, sha256: SHA, added_by: 'agent', at: '2026-10-23T10:00:00Z', ...over }) as Artifact
const data = (over: Partial<ArtifactContent> = {}): ArtifactContent => ({ name: 'x', size: 100, digest: 'sha256:' + SHA, verified: true, truncated: false, binary: false, text: '', ...over })

function thumb(a: Artifact, d: ArtifactContent | undefined) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  client.setQueryData(['workspaces'], [{ id: 'w1', prefix: 'DEMO', name: 'Demo', members: [], gates: {}, addons: {}, counts: {}, needs_you: 0 }])
  if (d) client.setQueryData(['artifact-text', 'w1', 'DEMO-1', a.name, SHA], d)
  return render(<QueryClientProvider client={client}><EvidenceThumb a={a} ticket="DEMO-1" /></QueryClientProvider>)
}

const TEXT = Array.from({ length: 12 }, (_, i) => `line ${i + 1}`).join('\n')

it('a text file shows its first 8 lines as text, the rest is not drawn', () => {
  thumb(art('notes.log'), data({ text: TEXT }))
  const t = screen.getByTestId('thumb-text')
  expect(t).toHaveTextContent('line 8')
  expect(t).not.toHaveTextContent('line 9')
})

it('a markdown thumbnail is plain text lines with # lines bold, never a rendered heading', () => {
  thumb(art('fact-check.md'), data({ text: '# Title\nbody <b>x</b>' }))
  const t = screen.getByTestId('thumb-text')
  expect(t.querySelector('h1, b, strong')).toBeNull()
  expect(t).toHaveTextContent('# Title')
  expect(t.firstElementChild!.className).toContain('font-bold')
})

it('SVG, GIF, HTML, PDF, a digest mismatch and an oversized file get the icon, never a thumbnail', () => {
  for (const n of ['a.svg', 'b.gif', 'c.html', 'd.pdf']) {
    const { unmount } = thumb(art(n), data({ text: '<svg onload=alert(1)>' }))
    expect(screen.queryByTestId('thumb-text')).toBeNull()
    expect(document.querySelector('img, iframe')).toBeNull()
    unmount()
  }
  thumb(art('e.log'), data({ text: 'x', digest: 'sha256:' + 'b'.repeat(64) }))
  expect(screen.queryByTestId('thumb-text')).toBeNull()
})

it('an oversized file is not read at all', () => {
  thumb(art('big.log', { bytes: 300 * 1024 }), data({ text: 'x' }))
  expect(screen.queryByTestId('thumb-text')).toBeNull()
})

it('every type has a text label on its badge', () => {
  expect(typeBadge(art('a.json')).label).toBe('JSON')
  expect(typeBadge(art('a.py')).label).toBe('PY')
  expect(typeBadge(art('noext')).label).toBe('OTHER')
})


it('receipts and task logs have a neutral title and no type badge; only host attestation yields passed', () => {
  for (const kind of ['receipt', 'log'] as const) {
    const a = art('T1-receipt.log', { kind, task: 'T1', added_by_kind: 'agent' })
    expect(cardTitle(a)).toBe("The agent's check")
    expect(typeBadge(a).label).toBe('')
    expect(checkOf(a, [{ id: 'T1', receipt: { exit: 0, ms: 1, attested_by: 'host' } }])).toBeNull()
  }
  const host = art('T1-receipt.log', { kind: 'receipt', task: 'T1', added_by_kind: 'host' })
  expect(checkOf(host, [{ id: 'T1', receipt: { exit: 0, ms: 1, attested_by: 'host' } }])).toBe('passed')
  expect(checkOf(host, [{ id: 'T1', receipt: { exit: 0, ms: 1, attested_by: 'agent' } }])).toBeNull()
})
