import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { renderApp } from '@/test/renderApp'
import { CATALOG, PROPOSED_NOTE } from '@/api/widgetCatalog'
import { CORE_TYPES } from '@/app/pages/ticket/widgets/parse'

describe('widgets addon page', () => {
  it('lists the templates with their pins and the core types, behind the A badge', async () => {
    renderApp('/addon/widgets/widgets')
    const frame = await waitFor(() => {
      const el = document.querySelector('[data-addon="widgets"]')
      expect(el).toBeTruthy()
      return el as HTMLElement
    }, { timeout: 5000 })
    expect((await within(frame).findAllByText('before-after@1', { selector: 'code' })).length).toBe(1)
    expect(within(frame).getByText('line-chart@1', { selector: 'code' })).toBeInTheDocument()
    expect(within(frame).getByText('option-prototype@1', { selector: 'code' })).toBeInTheDocument()
    expect(within(frame).getAllByText(/^[0-9a-f]{12}/).length).toBeGreaterThanOrEqual(3)
    expect(within(frame).getByText('kv', { selector: 'code' })).toBeInTheDocument()
    expect(screen.getAllByRole('img', { name: /From addon: widgets/ }).length).toBeGreaterThan(0)
  })

  it('is a gallery: a jump list, Core before Templates, one card per catalog entry, proposed types marked', async () => {
    const { user } = renderApp('/addon/widgets/widgets')
    const nav = await screen.findByRole('navigation', { name: 'Jump to a widget' }, { timeout: 5000 })
    expect(within(nav).getAllByRole('button').map((b) => b.textContent)).toEqual(CATALOG.map((c) => c.ref))
    await waitFor(() => expect(document.querySelectorAll('figure[data-widget]').length).toBe(CATALOG.length), { timeout: 5000 })
    expect(document.querySelectorAll('[data-state="refused"]')).toHaveLength(0)
    const core = screen.getByRole('heading', { level: 2, name: `Core types (${CORE_TYPES.length})` })
    const templates = screen.getByRole('heading', { level: 2, name: /Templates/ })
    expect(core.compareDocumentPosition(templates)).toBe(Node.DOCUMENT_POSITION_FOLLOWING)
    expect(screen.getAllByText(PROPOSED_NOTE)).toHaveLength(CATALOG.filter((c) => c.proposed).length)
    expect(screen.getAllByText(/Where it's allowed:/)).toHaveLength(CATALOG.length)
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    await user.click(within(nav).getByRole('button', { name: 'flow@1' }))
    expect(scroll).toHaveBeenCalled()
    expect(scroll.mock.contexts[0]).toBe(document.querySelector('figure[data-widget="ex-flow"]'))
  })
})
