import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { blockText, CATALOG } from '@/api/widgetCatalog'
import { AddonNode } from './AddonNode'
import { parseNode } from './nodes'

// The `widget` node draws one ticket widget through core's strict parser. Outside a workspace where the widgets
// addon is active (here: no workspace at all), a template never gets a frame.
const ex = (ref: string) => blockText(CATALOG.find((c) => c.ref === ref)!.example)

describe('widget node', () => {
  it('is in the closed node set, with a 64 KiB cap on the block', () => {
    expect(parseNode({ type: 'widget', block: '{}' })).toMatchObject({ ok: true, node: { source: false } })
    expect(parseNode({ type: 'widget', block: 'x'.repeat(64 * 1024 + 1) }).ok).toBe(false)
    expect(parseNode({ type: 'widget', block: '{}', html: '<b>' }).ok).toBe(true) // zod strips unknown node keys; the block is what is drawn
  })
  it('draws a core type with no frame', async () => {
    render(<AddonNode addon="widgets" node={{ type: 'widget', block: ex('metric') }} />)
    await waitFor(() => expect(document.querySelector('figure[data-widget="ex-metric"]')).toBeTruthy())
    expect(document.querySelector('iframe')).toBeNull()
  })
  it('keeps a template closed when the widgets addon is not active', async () => {
    render(<AddonNode addon="other" node={{ type: 'widget', block: ex('flow@1') }} />)
    await waitFor(() => expect(document.querySelector('figure[data-widget="ex-flow"]')).toBeTruthy())
    expect(document.querySelector('iframe')).toBeNull()
    expect(screen.getByText(/Agent HTML is off/)).toBeInTheDocument()
  })
  it('a broken block is refused with plain words, never thrown', async () => {
    render(<AddonNode addon="widgets" node={{ type: 'widget', block: '{"type":"callout","role":"note","text":"x","__proto__":{"y":1}}' }} />)
    await waitFor(() => expect(document.querySelector('[data-state="refused"]')).toBeTruthy())
    expect(screen.getByRole('alert')).toHaveTextContent('This widget uses a setting orch does not know ("__proto__")')
  })
})
