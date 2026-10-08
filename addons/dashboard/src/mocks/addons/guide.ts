import type { HelpRoute } from '@/api/guide'
import { GUIDE_PAGES } from './guide-pages'
import { registerAddon } from './registry'

// guide: how to use the dashboard, as eight markdown pages. The pages are fixed text, the same for everyone, so the
// only state is per viewer: which page they have open (`state.nav[viewer] = { current }`). `open` is minRole 'viewer'
// in the manifest. The view also sends the route-to-page map and the page texts, which core's `?` help sheet reads:
// core picks the page for the current route (src/api/guide.ts).

const ROUTES: HelpRoute[] = [
  { match: '/', page: 'getting-around' },
  { match: '/board', page: 'tickets-and-sections' },
  { match: '/tickets*', page: 'tickets-and-sections' },
  { match: '/ticket/*', page: 'gates-and-approvals' },
  { match: '/agents', page: 'agents-and-grants' },
  { match: '/settings/addons', page: 'addons' },
  { match: '/settings/addon/*', page: 'addons' },
  { match: '/addon/*', page: 'addons' },
]

type Nav = Record<string, { current?: string }>
const navOf = (state: Record<string, unknown>) => (state.nav ??= {}) as Nav

registerAddon({
  name: 'guide',
  seed: () => ({ settings: {}, nav: {} }),
  view(state, { viewer }) {
    const cur = GUIDE_PAGES.find((p) => p.slug === ((state.nav ?? {}) as Nav)[viewer]?.current) ?? GUIDE_PAGES[0]
    return {
      pages: GUIDE_PAGES,
      items: GUIDE_PAGES.map((p) => ({
        title: p.title,
        ...(p.slug === cur.slug ? { badge: 'Open' } : {}),
        actions: [{ action: 'open', label: 'Open', args: { slug: p.slug } }],
      })),
      current: cur,
      help: { routes: ROUTES, pages: GUIDE_PAGES },
    }
  },
  actions: {
    open({ state, body, viewer }) {
      const p = GUIDE_PAGES.find((x) => x.slug === body.slug)
      if (!p) return { ok: true, message: 'Pick a page to open.' }
      navOf(state)[viewer] = { current: p.slug }
      return { ok: true, message: `Opened ${p.title}.`, changed: true }
    },
  },
})
