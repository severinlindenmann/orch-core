import { createBrowserHistory, createMemoryHistory, createRootRouteWithContext, createRoute, createRouter, redirect } from '@tanstack/react-router'
import { Shell } from './shell/Shell'
import { lazyPage } from './pages/lazyPage'
import { SETTINGS_TABS } from './pages/settings/tabs'
import { validateTicketsSearch } from './pages/tickets/search'
import { validateArtifactsSearch, validateBoardSearch, validateTicketSearch } from './search'
import { workspaceRewrite, type UrlState } from './urls'

// Every page is its own chunk (the Shell shows a skeleton while it loads).
const TodayPage = lazyPage(() => import('./pages/today'), 'TodayPage')
const BoardPage = lazyPage(() => import('./pages/board'), 'BoardPage')
const TicketsPage = lazyPage(() => import('./pages/tickets'), 'TicketsPage')
const NewTicketPage = lazyPage(() => import('./pages/new-ticket'), 'NewTicketPage')
const TicketPage = lazyPage(() => import('./pages/ticket'), 'TicketPage')
const ArtifactsPage = lazyPage(() => import('./pages/artifacts'), 'ArtifactsPage')
const AgentsPage = lazyPage(() => import('./pages/agents'), 'AgentsPage')
const SettingsPage = lazyPage(() => import('./pages/settings'), 'SettingsPage')
const AddonPage = lazyPage(() => import('./pages/AddonPage'), 'AddonPage')

// Code-based route tree with short in-app paths. The address bar carries the workspace (`/w/DEMO/board`): the router's
// rewrite (urls.ts) strips it on the way in and adds the current one on the way out. Browser history (real paths) in
// the app; memory history only where a test passes an initial path.
export interface RouterContext {
  urls: UrlState
}
const rootRoute = createRootRouteWithContext<RouterContext>()({ component: Shell })

const todayRoute = createRoute({ getParentRoute: () => rootRoute, path: '/', component: TodayPage })
const boardRoute = createRoute({ getParentRoute: () => rootRoute, path: 'board', validateSearch: validateBoardSearch, component: BoardPage })
const ticketsRoute = createRoute({
  getParentRoute: () => rootRoute, path: 'tickets',
  validateSearch: validateTicketsSearch,
  component: TicketsPage,
})
const newTicketRoute = createRoute({ getParentRoute: () => rootRoute, path: 'tickets/new', component: NewTicketPage })
const ticketRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'ticket/$key',
  validateSearch: validateTicketSearch,
  component: function TicketRoute() {
    const { key } = ticketRoute.useParams()
    return <TicketPage ticketKey={key} />
  },
})
const artifactsRoute = createRoute({ getParentRoute: () => rootRoute, path: 'artifacts', validateSearch: validateArtifactsSearch, component: ArtifactsPage })
const agentsRoute = createRoute({ getParentRoute: () => rootRoute, path: 'agents', component: AgentsPage })
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
  component: function SettingsTabRoute() {
    const { tab } = settingsTabRoute.useParams()
    return <SettingsPage tab={tab} />
  },
})
const settingsAddonRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'settings/addon/$name',
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
  component: function AddonRoute() {
    const { name, page } = addonRoute.useParams()
    return <AddonPage name={name} page={page} />
  },
})

const routeTree = rootRoute.addChildren([todayRoute, boardRoute, ticketsRoute, newTicketRoute, ticketRoute, artifactsRoute, agentsRoute, settingsRoute, settingsTabRoute, settingsAddonRoute, settingsAddonsAliasRoute, addonRoute])

/**
 * The app's router. Without `initialPath` it owns the address bar (browser history); tests pass an in-app or
 * address-bar path (`/board`, `/w/DEMO/board`) and get memory history starting there.
 */
export function createAppRouter(initialPath?: string) {
  const urls: UrlState = { prefix: null }
  const history = initialPath === undefined ? createBrowserHistory() : createMemoryHistory({ initialEntries: [initialPath] })
  return createRouter({ routeTree, history, context: { urls }, rewrite: workspaceRewrite(urls) })
}

declare module '@tanstack/react-router' {
  interface Register {
    router: ReturnType<typeof createAppRouter>
  }
}
