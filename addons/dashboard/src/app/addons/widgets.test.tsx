import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { renderApp } from '@/test/renderApp'
import { CATALOG, fenced } from '@/api/widgetCatalog'
import { CORE_TYPES } from '@/app/pages/ticket/widgets/parse'

describe('widgets addon page', () => {
  it('lists the templates with their pins and the core types, behind the A badge', async () => {
    renderApp('/addon/widgets/widgets')
    const frame = await waitFor(() => {
      const el = document.querySelector('[data-addon="widgets"]')
      expect(el).toBeTruthy()
      return el as HTMLElement
    }, { timeout: 5000 })
    expect(await within(frame).findByText('before-after@1')).toBeInTheDocument()
    expect(within(frame).getByText('line-chart@1')).toBeInTheDocument()
    expect(within(frame).getByText('option-prototype@1')).toBeInTheDocument()
    expect(within(frame).getAllByText(/^[0-9a-f]{12}/).length).toBeGreaterThanOrEqual(3)
    expect(within(frame).getByText('kv')).toBeInTheDocument()
    expect(screen.getAllByRole('img', { name: /From addon: widgets/ }).length).toBeGreaterThan(0)
  })

  it('is a gallery: every type drawn by core with its source, Core before Templates, with where it is allowed', async () => {
    const { user } = renderApp('/addon/widgets/widgets')
    await screen.findByRole('heading', { level: 2, name: `Core types (${CORE_TYPES.length})` }, { timeout: 8000 })
    await waitFor(() => expect(document.querySelectorAll('figure[data-widget]').length).toBe(CATALOG.length), { timeout: 8000 })
    expect(document.querySelectorAll('[data-state="refused"]')).toHaveLength(0)
    const core = document.querySelector('figure[data-widget="ex-metric"]')!
    expect(core).toHaveAttribute('data-layer', 'core')
    expect(core.querySelector('iframe')).toBeNull()
    const frames = [...document.querySelectorAll('iframe')]
    expect(frames).toHaveLength(CATALOG.filter((c) => c.kind === 'template').length)
    for (const f of frames) expect(f.getAttribute('sandbox')).toBe('allow-scripts')
    expect(screen.getAllByText(/Where it's allowed:/).length).toBe(CATALOG.length)
    expect(screen.getByRole('heading', { level: 2, name: /Templates/ }).compareDocumentPosition(core)).toBe(Node.DOCUMENT_POSITION_PRECEDING)
    const sources = document.querySelectorAll('[data-widget-source]')
    expect(sources).toHaveLength(CATALOG.length)
    const writeText = vi.fn(() => Promise.resolve())
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    const first = sources[0] as HTMLElement
    await user.click(within(first).getByRole('button', { name: 'Copy source' }))
    expect(writeText).toHaveBeenCalledWith(fenced(CATALOG[0].example))
    expect(await within(first).findByText('Copied')).toBeInTheDocument()
  }, 20000)
})
