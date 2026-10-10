// G4 calm navigation: the router keeps the old page until the new one is ready (or shows its skeleton after
// PENDING_MS), a new page starts at the top while a filter change keeps the scroll, the page fades in once (never
// under reduced motion), and Today appears in one piece.
import { act, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { PENDING_MS } from './router'
import { FADE_MS } from './shell/pageMotion'

const T = { timeout: 8000 }
const sleep = (ms: number) => act(() => new Promise<void>((r) => setTimeout(r, ms)))
const loading = () => screen.queryByRole('status', { name: 'Loading page' })

afterEach(() => vi.restoreAllMocks())

describe('the router owns page loading', () => {
  it('keeps the old page until PENDING_MS, then shows the page skeleton, then the page', async () => {
    await import('./pages/agents') // the chunk is in: only the data is slow
    // A wider pending delay than the app's, so a loaded test machine cannot blur "before" and "after" it.
    const pendingMs = 800
    const { router } = renderApp('/board', { pendingMs })
    await screen.findByTestId('card-DEMO-0043', {}, T)
    let release!: () => void
    const gate = new Promise<void>((r) => (release = r))
    const real = api.getAgentActivity.bind(api)
    vi.spyOn(api, 'getAgentActivity').mockImplementation(async (ws) => {
      await gate
      return real(ws)
    })
    // When the skeleton first shows, measured from the navigation (timers in a loaded test run can overshoot, so the
    // time is taken by an observer, not by sleeping to just under PENDING_MS).
    let shownAt: number | null = null
    const watch = new MutationObserver(() => {
      if (shownAt === null && loading()) shownAt = performance.now()
    })
    watch.observe(document.body, { childList: true, subtree: true })
    const start = performance.now()
    act(() => void router.navigate({ to: '/agents' }))
    await sleep(100)
    // Well under the pending delay: the board is still there.
    expect(screen.getByTestId('card-DEMO-0043')).toBeVisible()
    await waitFor(() => expect(loading()).toBeInTheDocument(), T)
    watch.disconnect()
    expect(shownAt! - start).toBeGreaterThanOrEqual(pendingMs - 5)
    // Past it: one skeleton in the page's shape (its heading included), never a blank page.
    expect(loading()).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Agents', level: 1 })).toBeInTheDocument()
    // (React keeps the old page mounted but hidden until the new one is in.)
    expect(screen.queryByTestId('card-DEMO-0043')).not.toBeVisible()
    release()
    expect(await screen.findByText(/agent sessions? ·/, {}, T)).toBeInTheDocument()
    expect(loading()).toBeNull()
  })

  it('a page that is ready within PENDING_MS replaces the old one directly, with no skeleton in between', async () => {
    await import('./pages/agents')
    // The same rule with a wider delay: a page that is ready before it shows at once (the app uses PENDING_MS).
    const { router } = renderApp('/board', { pendingMs: 1500 })
    await screen.findByTestId('card-DEMO-0043', {}, T)
    const seen: string[] = []
    const watch = new MutationObserver(() => {
      if (loading()) seen.push('skeleton')
      if (!document.querySelector('main')?.textContent?.trim()) seen.push('blank')
    })
    watch.observe(document.body, { childList: true, subtree: true, characterData: true })
    act(() => void router.navigate({ to: '/agents' }))
    expect(await screen.findByText(/agent sessions? ·/, {}, T)).toBeInTheDocument()
    watch.disconnect()
    expect(seen).toEqual([])
  })

  it('Today appears in one piece: the queue, the Connections group and the Glance arrive together', async () => {
    const states = new Set<string>()
    const watch = new MutationObserver(() => {
      const main = document.querySelector('main')
      if (!main) return
      const queue = !!main.querySelector('section[aria-label="Needs you"]') && /Approvals/.test(main.textContent ?? '')
      if (!queue) return
      const connections = /Connections · 2/.test(main.textContent ?? '')
      const glance = !!main.querySelector('#glance-h')
      states.add(`connections=${connections} glance=${glance}`)
    })
    watch.observe(document.body, { childList: true, subtree: true, characterData: true })
    renderApp('/', { viewer: 'p_sev' })
    expect(await screen.findByText(/Re-login needed: databricks-prod/, {}, T)).toBeInTheDocument()
    await waitFor(() => expect(document.querySelector('#glance-h')).toBeInTheDocument(), T)
    watch.disconnect()
    expect([...states]).toEqual(['connections=true glance=true'])
  })
})

describe('scroll on page changes', () => {
  it('starts a new page at the top, keeps the scroll for a filter change, and restores it on Back', async () => {
    const { router } = renderApp('/tickets')
    await screen.findByText('DEMO-0043', {}, T)
    const main = document.getElementById('main')!
    const scrollTo = (px: number) => {
      main.scrollTop = px
      main.dispatchEvent(new Event('scroll'))
    }
    scrollTo(300)
    expect(main.scrollTop).toBe(300)
    // A filter (search only): same page, same scroll.
    await act(() => router.navigate({ to: '/tickets', search: { q: 'dbt' } }))
    await sleep(50)
    expect(main.scrollTop).toBe(300)
    // Another page: the top.
    await act(() => router.navigate({ to: '/board' }))
    await screen.findByTestId('card-DEMO-0043', {}, T)
    expect(main.scrollTop).toBe(0)
    // Back: where the list was left.
    act(() => router.history.back())
    await screen.findByText('DEMO-0043', {}, T)
    await waitFor(() => expect(main.scrollTop).toBe(300), T)
  })
})

describe('scroll while the next page is pending (G4 review I1)', () => {
  it('the skeleton starts at the top, and Back restores the offset the page was left at, not a clamped one', async () => {
    await import('./pages/agents')
    const { router } = renderApp('/tickets')
    await screen.findByText('DEMO-0043', {}, T)
    const main = document.getElementById('main')!
    main.scrollTop = 300
    main.dispatchEvent(new Event('scroll'))
    let release!: () => void
    const gate = new Promise<void>((r) => (release = r))
    const real = api.getAgentActivity.bind(api)
    vi.spyOn(api, 'getAgentActivity').mockImplementation(async (ws) => {
      await gate
      return real(ws)
    })
    act(() => void router.navigate({ to: '/agents' }))
    await sleep(PENDING_MS + 100)
    expect(loading()).toBeInTheDocument()
    expect(main.scrollTop).toBe(0)
    // What a browser does when the hidden old page leaves a short skeleton: the scroll is clamped (and reported).
    main.scrollTop = 40
    main.dispatchEvent(new Event('scroll'))
    main.scrollTop = 0
    release()
    await screen.findByText(/agent sessions? ·/, {}, T)
    expect(main.scrollTop).toBe(0)
    act(() => router.history.back())
    await screen.findByText('DEMO-0043', {}, T)
    await waitFor(() => expect(main.scrollTop).toBe(300), T)
  })
})

describe('the page fade', () => {
  const realMatchMedia = window.matchMedia
  const motion = (reduce: boolean) => {
    window.matchMedia = ((q: string) => ({ matches: reduce && q.includes('reduce'), media: q, addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia
  }
  const animate = vi.fn((..._args: unknown[]) => ({ cancel() {} }) as unknown as Animation)
  afterEach(() => {
    window.matchMedia = realMatchMedia
    animate.mockClear()
    delete (HTMLElement.prototype as Partial<HTMLElement>).animate
  })
  const fades = () => animate.mock.calls.filter(([frames]) => JSON.stringify(frames) === JSON.stringify([{ opacity: 0 }, { opacity: 1 }]))

  it('fades a new page in once, opacity only, in FADE_MS; not on a filter or a settings section change', async () => {
    motion(false)
    HTMLElement.prototype.animate = animate as unknown as HTMLElement['animate']
    const { router } = renderApp('/board')
    await screen.findByTestId('card-DEMO-0043', {}, T)
    expect(fades()).toHaveLength(0) // not on the app's first paint
    await act(() => router.navigate({ to: '/tickets' }))
    await screen.findByText('DEMO-0043', {}, T)
    expect(fades()).toHaveLength(1)
    expect(fades()[0]).toEqual([[{ opacity: 0 }, { opacity: 1 }], expect.objectContaining({ duration: FADE_MS })])
    expect(FADE_MS).toBeLessThanOrEqual(160)
    await act(() => router.navigate({ to: '/tickets', search: { q: 'dbt' } }))
    await sleep(50)
    expect(fades()).toHaveLength(1)
    await act(() => router.navigate({ to: '/settings/$tab', params: { tab: 'general' } }))
    await screen.findByRole('link', { name: 'Members' }, T)
    expect(fades()).toHaveLength(2)
    await act(() => router.navigate({ to: '/settings/$tab', params: { tab: 'members' } }))
    await screen.findByRole('heading', { name: 'Members & roles' }, T)
    expect(fades()).toHaveLength(2)
  })

  it('does not fade under reduced motion', async () => {
    motion(true)
    HTMLElement.prototype.animate = animate as unknown as HTMLElement['animate']
    const { router } = renderApp('/board')
    await screen.findByTestId('card-DEMO-0043', {}, T)
    await act(() => router.navigate({ to: '/tickets' }))
    await screen.findByText('DEMO-0043', {}, T)
    expect(fades()).toHaveLength(0)
  })
})

describe('pages say when they could not load (G4 review M5)', () => {
  it('Today, Agents and Settings show an error with Retry when the viewer cannot be read, and Retry recovers', async () => {
    for (const [path, what, after] of [
      ['/', /Could not load Today/, /need you|Nothing needs you/],
      ['/agents', /Could not load agents/, /agent sessions? ·/],
      ['/settings/general', /Could not load settings/, /Members/],
    ] as const) {
      const real = api.getMe.bind(api)
      const spy = vi.spyOn(api, 'getMe').mockRejectedValue(new Error('down'))
      const { user, unmount } = renderApp(path)
      expect(await screen.findByRole('alert', {}, T)).toHaveTextContent(what)
      spy.mockImplementation(real)
      await user.click(screen.getByRole('button', { name: 'Retry' }))
      expect((await screen.findAllByText(after, {}, T)).length).toBeGreaterThan(0)
      spy.mockRestore()
      unmount()
    }
  })
})

describe('a workspace switch is another page (G4 review M7)', () => {
  it('samePage: the workspace counts only when both addresses name one', async () => {
    const { samePage } = await import('./shell/pageMotion')
    expect(samePage('|/board', 'DEMO|/board')).toBe(true)
    expect(samePage('DEMO|/board', 'OPS|/board')).toBe(false)
    expect(samePage('DEMO|/board', 'DEMO|/tickets')).toBe(false)
  })
})

describe('Today fix round 2 (G4 re-review R1, R2)', () => {
  it('R1: an owner whose connections fail sees Could not load with Retry, not a skeleton for good', async () => {
    const real = api.getConnections.bind(api)
    const spy = vi.spyOn(api, 'getConnections').mockRejectedValue(new Error('down'))
    const { user } = renderApp('/', { viewer: 'p_sev' })
    expect(await screen.findByRole('alert', {}, T)).toHaveTextContent(/Could not load Today/)
    spy.mockImplementation(real)
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText(/Re-login needed: databricks-prod/, {}, T)).toBeInTheDocument()
  })

  it('R2: when the addons list arrives late, Today still appears with its Glance, not without it first', async () => {
    let release!: () => void
    const gate = new Promise<void>((r) => (release = r))
    const real = api.getAddons.bind(api)
    vi.spyOn(api, 'getAddons').mockImplementation(async () => {
      await gate
      return real()
    })
    const states = new Set<string>()
    const watch = new MutationObserver(() => {
      const main = document.querySelector('main')
      if (!main || !main.querySelector('section[aria-label="Needs you"]')) return
      states.add(`glance=${!!main.querySelector('#glance-h')}`)
    })
    watch.observe(document.body, { childList: true, subtree: true })
    renderApp('/', { viewer: 'p_sev' })
    // Past the loaders' cap (they run in parallel): Today mounts while the addons are still missing, and waits for
    // them (within its own cap of LOADER_WAIT_MS from mount).
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Today', level: 1 })).toBeInTheDocument(), T)
    await sleep(500)
    release()
    await waitFor(() => expect(document.querySelector('#glance-h')).toBeInTheDocument(), T)
    watch.disconnect()
    expect([...states]).toEqual(['glance=true'])
  }, 20000)
})
