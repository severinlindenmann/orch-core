import { createMemoryHistory, createRootRoute, createRoute, createRouter } from '@tanstack/react-router'
import { Shell } from './shell/Shell'
import { AddonPage } from './pages/AddonPage'
import { Placeholder } from './pages/Placeholders'
import { TodayPage } from './pages/today'
import { BoardPage } from './pages/board'
import { TicketPage } from './pages/ticket'
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
const ticketRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'ticket/$key',
  component: function TicketRoute() {
    const { key } = ticketRoute.useParams()
    return <TicketPage ticketKey={key} />
  },
})
const agentsRoute = createRoute({ getParentRoute: () => rootRoute, path: 'agents', component: () => <Placeholder title="Agents" /> })
const settingsRoute = createRoute({ getParentRoute: () => rootRoute, path: 'settings', component: () => <Placeholder title="Settings" /> })
const addonRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'addon/$name/$page',
  component: function AddonRoute() {
    const { name, page } = addonRoute.useParams()
    return <AddonPage name={name} page={page} />
  },
})

const routeTree = rootRoute.addChildren([todayRoute, boardRoute, ticketsRoute, ticketRoute, agentsRoute, settingsRoute, addonRoute])

export function createAppRouter(initialPath = '/') {
  return createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [initialPath] }) })
}

declare module '@tanstack/react-router' {
  interface Register {
    router: ReturnType<typeof createAppRouter>
  }
}
