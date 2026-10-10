import { act, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderApp } from '@/test/renderApp'

// jsdom lays nothing out: the page "measures" `width` here (1200 px: a 1440 px window with the sidebar open). Tests
// change it to cross the pane/drawer threshold, as resizing the window or the terminal dock does.
const page = vi.hoisted(() => ({ width: 1200, subs: new Set<() => void>() }))
vi.mock('@/lib/useElementWidth', async () => {
  const { useSyncExternalStore } = await import('react')
  return {
    useElementWidth: () => [
      () => {},
      useSyncExternalStore(
        (cb: () => void) => {
          page.subs.add(cb)
          return () => page.subs.delete(cb)
        },
        () => page.width,
      ),
    ],
  }
})
const setPageWidth = (w: number) =>
  act(() => {
    page.width = w
    page.subs.forEach((cb) => cb())
  })

const T = { timeout: 4000 }
const original = window.matchMedia
const wide = (on: boolean) => {
  window.matchMedia = ((query: string) => ({
    matches: (on && query === '(min-width: 1280px)') || query.includes('prefers-reduced-motion: reduce'),
    media: query,
    onchange: null,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
    dispatchEvent: () => false,
  })) as typeof window.matchMedia
}

beforeEach(() => {
  localStorage.clear()
  page.width = 1200
})
afterEach(() => {
  window.matchMedia = original
})

const currentItem = () => document.querySelector('[data-artifact][aria-current="true"]')
const pane = (name: string) => screen.findByRole('complementary', { name: `Preview of ${name}` }, T)
const previewButtons = () => [...document.querySelectorAll<HTMLElement>('[data-artifact] [data-preview]')]
const nameOf = (b: HTMLElement) => b.getAttribute('aria-label')!.replace(/^Preview /, '')

describe('Artifacts preview pane (a wide page)', () => {
  for (const view of ['list', 'grid'] as const) {
    it(`${view}: Preview shows the artifact beside the results, not in a drawer`, async () => {
      wide(true)
      const { user } = renderApp('/artifacts', { storage: { 'orch.artifacts.view.p_sev': view } })
      expect(await screen.findByText('Select Preview on an artifact to see it here.', {}, T)).toBeInTheDocument()
      await user.click(await screen.findByRole('button', { name: 'Preview tariff-export.log' }, T))
      const p = await pane('tariff-export.log')
      expect(screen.queryByRole('dialog')).toBeNull()
      expect(within(p).getByRole('link', { name: 'DEMO-0043' })).toBeInTheDocument()
      expect(await within(p).findByRole('button', { name: /Copy all/ }, T)).toBeInTheDocument()
      expect(currentItem()).toHaveTextContent('tariff-export.log')
    })
  }
  it('the list beside the pane keeps Name, Kind, Ticket and Added', async () => {
    wide(true)
    renderApp('/artifacts')
    await screen.findByRole('button', { name: 'Preview tariff-export.log' }, T)
    expect(screen.queryByRole('columnheader', { name: 'Size' })).toBeNull()
  })
  it('an HTML report in the pane runs in the sandboxed frame, as in the drawer', async () => {
    wide(true)
    const { user } = renderApp('/artifacts')
    await user.click(await screen.findByRole('button', { name: 'Preview reconciliation-demo.html' }, T))
    const p = await pane('reconciliation-demo.html')
    expect(await within(p).findByTitle(/Sandboxed preview of reconciliation-demo.html/, {}, T)).toHaveAttribute('sandbox', 'allow-scripts')
  })
  it('closing the pane puts focus back on the item’s Preview button', async () => {
    wide(true)
    const { user } = renderApp('/artifacts')
    const open = await screen.findByRole('button', { name: 'Preview tariff-export.log' }, T)
    await user.click(open)
    await user.click(await screen.findByRole('button', { name: 'Close the preview' }, T))
    expect(screen.queryByRole('complementary', { name: /Preview of/ })).toBeNull()
    await waitFor(() => expect(open).toHaveFocus())
    expect(currentItem()).toBeNull()
  })
  it('the preview keeps its artifact from the list to the grid', async () => {
    wide(true)
    const { user } = renderApp('/artifacts')
    await user.click(await screen.findByRole('button', { name: 'Preview tariff-export.log' }, T))
    await pane('tariff-export.log')
    await user.click(screen.getByRole('radio', { name: 'Grid' }))
    expect(await screen.findByRole('list', { name: 'Artifacts' })).toBeInTheDocument()
    expect(currentItem()?.tagName).toBe('LI')
    expect(currentItem()).toHaveTextContent('tariff-export.log')
    await pane('tariff-export.log')
  })
  it('a new search clears the preview: it belonged to the old results', async () => {
    wide(true)
    const { user } = renderApp('/artifacts')
    await user.click(await screen.findByRole('button', { name: 'Preview tariff-export.log' }, T))
    await pane('tariff-export.log')
    await user.type(screen.getByRole('searchbox', { name: 'Search artifacts' }), 'tolerance')
    expect(await screen.findByText('1 artifact', {}, T)).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('complementary', { name: /Preview of/ })).toBeNull())
    expect(currentItem()).toBeNull()
  })
})

describe('Artifacts preview: pane ↔ drawer', () => {
  it('a 720 px page beside the dock previews in the drawer, with the compact list', async () => {
    wide(true)
    page.width = 720
    const { user } = renderApp('/artifacts')
    await user.click(await screen.findByRole('button', { name: 'Preview tariff-export.log' }, T))
    expect(await screen.findByRole('dialog', {}, T)).toBeInTheDocument()
    expect(screen.queryByRole('complementary', { name: /Preview of/ })).toBeNull()
    expect(screen.queryByRole('columnheader', { name: 'Size' })).toBeNull()
  })
  it('narrowing moves the open preview into the drawer; widening moves it back, focus on its Preview button', async () => {
    wide(true)
    const { user } = renderApp('/artifacts')
    const open = await screen.findByRole('button', { name: 'Preview tariff-export.log' }, T)
    await user.click(open)
    await pane('tariff-export.log')
    setPageWidth(720)
    const sheet = await screen.findByRole('dialog', {}, T)
    expect(within(sheet).getByRole('heading', { name: 'tariff-export.log' })).toBeInTheDocument()
    setPageWidth(1200)
    await pane('tariff-export.log')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await waitFor(() => expect(open).toHaveFocus())
  })
})

describe('Artifacts preview: j and k', () => {
  for (const view of ['list', 'grid'] as const) {
    it(`${view}: j / k in the results move focus to the next / previous Preview, the pane follows and says so`, async () => {
      wide(true)
      const { user } = renderApp('/artifacts', { storage: { 'orch.artifacts.view.p_sev': view } })
      const first = await screen.findByRole('button', { name: 'Preview tariff-export.log' }, T)
      await user.click(first)
      await pane('tariff-export.log')
      const buttons = previewButtons()
      const at = buttons.indexOf(first)
      await user.keyboard('j')
      await waitFor(() => expect(buttons[at + 1]).toHaveFocus())
      await pane(nameOf(buttons[at + 1]))
      expect(screen.getByText(`Previewing ${nameOf(buttons[at + 1])}`)).toHaveAttribute('aria-live', 'polite')
      await user.keyboard('k')
      await waitFor(() => expect(first).toHaveFocus())
      await pane('tariff-export.log')
    })
  }
  it('j skips items without a preview (a web link, an addon’s artifact)', async () => {
    wide(true)
    const { user } = renderApp('/artifacts')
    await screen.findByRole('button', { name: 'Preview tariff-export.log' }, T)
    previewButtons()[0].focus()
    expect(previewButtons()[0]).toHaveAccessibleName('Preview seeds-in-warehouse.png')
    await user.keyboard('k')
    expect(previewButtons()[0]).toHaveFocus()
    await user.keyboard('j')
    await waitFor(() => expect(previewButtons()[1]).toHaveFocus())
  })
  it('j does nothing outside the results: in the search box, on the page, or in the pane', async () => {
    wide(true)
    const { user } = renderApp('/artifacts')
    await user.click(await screen.findByRole('button', { name: 'Preview tariff-export.log' }, T))
    const p = await pane('tariff-export.log')
    const before = currentItem()
    await user.type(screen.getByRole('searchbox', { name: 'Search artifacts' }), 'j')
    expect(currentItem()).toBe(before)
    await user.clear(screen.getByRole('searchbox', { name: 'Search artifacts' }))
    ;(await within(p).findByRole('button', { name: /Copy all/ }, T)).focus()
    await user.keyboard('j')
    expect(currentItem()).toBe(before)
  })
  it('a narrow page: j moves the focus only, it does not open the drawer', async () => {
    wide(false)
    const { user } = renderApp('/artifacts')
    const first = await screen.findByRole('button', { name: 'Preview tariff-export.log' }, T)
    first.focus()
    const buttons = previewButtons()
    await user.keyboard('j')
    await waitFor(() => expect(buttons[buttons.indexOf(first) + 1]).toHaveFocus())
    expect(screen.queryByRole('dialog')).toBeNull()
  })
  it('a handler that already took the key wins', async () => {
    wide(true)
    const { user } = renderApp('/artifacts')
    const first = await screen.findByRole('button', { name: 'Preview tariff-export.log' }, T)
    await user.click(first)
    const take = (e: KeyboardEvent) => e.preventDefault()
    document.addEventListener('keydown', take, { capture: true })
    try {
      await user.keyboard('j')
      expect(first).toHaveFocus()
    } finally {
      document.removeEventListener('keydown', take, { capture: true })
    }
  })
})
