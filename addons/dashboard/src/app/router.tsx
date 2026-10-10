import type { QueryClient } from '@tanstack/react-query'
import { createBrowserHistory, createMemoryHistory, createRootRouteWithContext, createRoute, createRouter, redirect } from '@tanstack/react-router'
import { Shell } from './shell/Shell'
import { RouteProblem } from './shell/RouteProblem'
import { lazyPage } from './pages/lazyPage'
import { AddonPageSkeleton, AgentsSkeleton, ArtifactsSkeleton, BoardSkeleton, GenericSkeleton, SettingsSkeleton, TicketSkeleton, TicketsSkeleton, TodaySkeleton } from './pages/skeletons'
import { SETTINGS_TABS } from './pages/settings/tabs'
import { validateTicketsSearch } from './pages/tickets/search'
import { addonPageData, agentsData, artifactsData, boardData, loadShell, pageLoader, settingsData, ticketData, ticketsData, todayData, type LoaderContext } from './routeData'
import { validateAgentsSearch, validateArtifactsSearch, validateBoardSearch, validateTicketSearch } from './search'
import { workspaceRewrite, type UrlState } from './urls'

// Every page is its own chunk. The route loaders load it (and warm the page's data) before the router shows the page:
// the old page stays until then, or until `PENDING_MS`, after which the page's skeleton shows (see routeData.ts).
const TodayPage = lazyPage(() => import('./pages/today'), 'TodayPage')
const BoardPage = lazyPage(() => import('./pages/board'), 'BoardPage')
const TicketsPage = lazyPage(() => import('./pages/tickets'), 'TicketsPage')
const NewTicketPage = lazyPage(() => import('./pages/new-ticket'), 'NewTicketPage')
const TicketPage = lazyPage(() => import('./pages/ticket'), 'TicketPage')
const ArtifactsPage = lazyPage(() => import('./pages/artifacts'), 'ArtifactsPage')
const AgentsPage = lazyPage(() => import('./pages/agents'), 'AgentsPage')
const SettingsPage = lazyPage(() => import('./pages/settings'), 'SettingsPage')
const AddonPage = lazyPage(() => import('./pages/AddonPage'), 'AddonPage')
/** The addon node renderers an addon page or a ticket's panels usually need (widgets, forms, markdown, charts). */
const addonNodes = () => import('@/addon-ui/preloadNodes').then((m) => m.preloadAddonNodes())

/** Under this a page simply replaces the old one; past it the page's skeleton shows, for at least PENDING_MIN_MS. */
export const PENDING_MS = 200
export const PENDING_MIN_MS = 300

// Code-based route tree with short in-app paths. The address bar carries the workspace (`/w/DEMO/board`): the router's
// rewrite (urls.ts) strips it on the way in and adds the current one on the way out. Browser history (real paths) in
// the app; memory history only where a test passes an initial path.
export interface RouterContext extends LoaderContext {
  urls: UrlState
}
// The shell's data loads before its first paint (nothing to keep on screen yet: the document's own background).
const rootRoute = createRootRouteWithContext<RouterContext>()({
  component: Shell,
  loader: ({ context }) => loadShell(context),
  pendingComponent: () => null,
  pendingMinMs: 0,
})

const todayRoute = createRoute({ getParentRoute: () => rootRoute, path: '/', component: TodayPage, loader: pageLoader(todayData, [TodayPage.preload]), pendingComponent: () => <TodaySkeleton /> })
const boardRoute = createRoute({ getParentRoute: () => rootRoute, path: 'board', validateSearch: validateBoardSearch, component: BoardPage, loader: pageLoader(boardData, [BoardPage.preload]), pendingComponent: BoardSkeleton })
const ticketsRoute = createRoute({
  getParentRoute: () => rootRoute, path: 'tickets',
  validateSearch: validateTicketsSearch,
  component: TicketsPage,
  // The search is read once, on entering the page: a filter change keeps the page and refetches in place.
  loader: ({ context, location }) => pageLoader(ticketsData(validateTicketsSearch(location.search as Record<string, unknown>)), [TicketsPage.preload])({ context }),
  pendingComponent: TicketsSkeleton,
})
const newTicketRoute = createRoute({ getParentRoute: () => rootRoute, path: 'tickets/new', component: NewTicketPage, loader: pageLoader(() => [], [NewTicketPage.preload]) })
const ticketRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'ticket/$key',
  validateSearch: validateTicketSearch,
  // The addon renderers a ticket's panels use load when the app is idle (Shell), not on every hovered ticket link.
  // A real visit waits for the addon renderers too (its panels then render without placeholders of another size);
  // a hover preload does not (moving the pointer down a list of tickets loads the ticket page and its data only).
  loader: ({ context, params, preload }) => pageLoader(ticketData(params.key), preload ? [TicketPage.preload] : [TicketPage.preload, addonNodes])({ context }),
  pendingComponent: function TicketPending() {
    const { key } = ticketRoute.useParams()
    return <TicketSkeleton title={key} />
  },
  component: function TicketRoute() {
    const { key } = ticketRoute.useParams()
    return <TicketPage ticketKey={key} />
  },
})
const artifactsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'artifacts',
  validateSearch: validateArtifactsSearch,
  component: ArtifactsPage,
  loader: pageLoader(artifactsData, [ArtifactsPage.preload]),
  pendingComponent: function ArtifactsPending() {
    // The layout the address asks for (the viewer's remembered one is not known yet: list).
    const { view } = artifactsRoute.useSearch()
    return <ArtifactsSkeleton view={view ?? 'list'} />
  },
})
const agentsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'agents',
  validateSearch: validateAgentsSearch,
  component: AgentsPage,
  loader: pageLoader(agentsData, [AgentsPage.preload]),
  pendingComponent: function AgentsPending() {
    const { tab } = agentsRoute.useSearch()
    return <AgentsSkeleton tab={tab ?? 'sessions'} />
  },
})
const settingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'settings',
  beforeLoad: () => {
    throw redirect({ to: '/settings/$tab', params: { tab: 'general' } })
  },
})
const settingsTabRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'settings/$tab',
  // An unknown tab (or /settings/addon without a name) would render only the sub-nav: go to General instead.
  beforeLoad: ({ params }) => {
    if (!SETTINGS_TABS.includes(params.tab)) throw redirect({ to: '/settings/$tab', params: { tab: 'general' } })
  },
  loader: ({ context, params }) => pageLoader(settingsData(params.tab), [SettingsPage.preload])({ context }),
  pendingComponent: SettingsSkeleton,
  component: function SettingsTabRoute() {
    const { tab } = settingsTabRoute.useParams()
    return <SettingsPage tab={tab} />
  },
})
const settingsAddonRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'settings/addon/$name',
  loader: ({ context, params }) => pageLoader(settingsData('addons', params.name), [SettingsPage.preload, addonNodes])({ context }),
  pendingComponent: SettingsSkeleton,
  component: function SettingsAddonRoute() {
    const { name } = settingsAddonRoute.useParams()
    return <SettingsPage addon={name} />
  },
})
// An older spelling of the addon settings link: the drawer lives at /settings/addon/$name.
const settingsAddonsAliasRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'settings/addons/$name',
  beforeLoad: ({ params }) => {
    throw redirect({ to: '/settings/addon/$name', params: { name: params.name } })
  },
})
const addonRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'addon/$name/$page',
  loader: ({ context, params }) => pageLoader(addonPageData(params.name), [AddonPage.preload, addonNodes])({ context }),
  pendingComponent: () => <AddonPageSkeleton />,
  component: function AddonRoute() {
    const { name, page } = addonRoute.useParams()
    return <AddonPage name={name} page={page} />
  },
})

const routeTree = rootRoute.addChildren([todayRoute, boardRoute, ticketsRoute, newTicketRoute, ticketRoute, artifactsRoute, agentsRoute, settingsRoute, settingsTabRoute, settingsAddonRoute, settingsAddonsAliasRoute, addonRoute])

/**
 * The app's router. Without `initialPath` it owns the address bar (browser history); tests pass an in-app or
 * address-bar path (`/board`, `/w/DEMO/board`) and get memory history starting there. With `queryClient` the route
 * loaders warm each page's data before it shows (without one they load the page's code only).
 */
export function createAppRouter(initialPath?: string, queryClient?: QueryClient, opts: { pendingMs?: number } = {}) {
  const urls: UrlState = { prefix: null }
  const history = initialPath === undefined ? createBrowserHistory() : createMemoryHistory({ initialEntries: [initialPath] })
  return createRouter({
    routeTree,
    history,
    context: { urls, queryClient },
    rewrite: workspaceRewrite(urls),
    // Hovering or focusing a link loads the page's code and data, so the click usually shows the page at once.
    defaultPreload: 'intent',
    // Long enough that moving the pointer down a list of ticket links does not load each of them.
    defaultPreloadDelay: 100,
    // The query cache decides freshness; the router keeps a preloaded result long enough for the click that follows.
    defaultPreloadStaleTime: 0,
    defaultPendingMs: opts.pendingMs ?? PENDING_MS,
    defaultPendingMinMs: PENDING_MIN_MS,
    defaultPendingComponent: GenericSkeleton,
    defaultErrorComponent: RouteProblem,
  })
}

declare module '@tanstack/react-router' {
  interface Register {
    router: ReturnType<typeof createAppRouter>
  }
}
