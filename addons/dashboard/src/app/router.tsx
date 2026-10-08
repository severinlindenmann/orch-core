import { createMemoryHistory, createRootRoute, createRoute, createRouter, redirect } from '@tanstack/react-router'
import { Shell } from './shell/Shell'
import { AddonPage } from './pages/AddonPage'
import { SETTINGS_TABS, SettingsPage } from './pages/settings'
import { TodayPage } from './pages/today'
import { BoardPage } from './pages/board'
import { TicketPage } from './pages/ticket'
import { NewTicketPage } from './pages/new-ticket'
import { AgentsPage } from './pages/agents'
import { TicketsPage } from './pages/tickets'
import { validateTicketsSearch } from './pages/tickets/search'

// Code-based route tree. Memory history on purpose: the app also runs inside a sandboxed viewer
// where URL fragments do not carry state.
const rootRoute = createRootRoute({ component: Shell })

const todayRoute = createRoute({ getParentRoute: () => rootRoute, path: '/', component: TodayPage })
const boardRoute = createRoute({ getParentRoute: () => rootRoute, path: 'board', component: BoardPage })
const ticketsRoute = createRoute({
  getParentRoute: () => rootRoute, path: 'tickets',
  validateSearch: validateTicketsSearch,
  component: TicketsPage,
})
const newTicketRoute = createRoute({ getParentRoute: () => rootRoute, path: 'tickets/new', component: NewTicketPage })
const ticketRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'ticket/$key',
  component: function TicketRoute() {
    const { key } = ticketRoute.useParams()
    return <TicketPage ticketKey={key} />
  },
})
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
const addonRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'addon/$name/$page',
  component: function AddonRoute() {
    const { name, page } = addonRoute.useParams()
    return <AddonPage name={name} page={page} />
  },
})

const routeTree = rootRoute.addChildren([todayRoute, boardRoute, ticketsRoute, newTicketRoute, ticketRoute, agentsRoute, settingsRoute, settingsTabRoute, settingsAddonRoute, addonRoute])

export function createAppRouter(initialPath = '/') {
  return createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [initialPath] }) })
}

declare module '@tanstack/react-router' {
  interface Register {
    router: ReturnType<typeof createAppRouter>
  }
}
