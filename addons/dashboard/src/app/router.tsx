import { createMemoryHistory, createRootRoute, createRoute, createRouter } from '@tanstack/react-router'
import { Shell } from './shell/Shell'
import { Placeholder, TodayPlaceholder } from './pages/Placeholders'

// Code-based route tree. Memory history on purpose: the app also runs inside a sandboxed viewer
// where URL fragments do not carry state.
const rootRoute = createRootRoute({ component: Shell })

const todayRoute = createRoute({ getParentRoute: () => rootRoute, path: '/', component: TodayPlaceholder })
const boardRoute = createRoute({ getParentRoute: () => rootRoute, path: 'board', component: () => <Placeholder title="Board" /> })
const ticketsRoute = createRoute({ getParentRoute: () => rootRoute, path: 'tickets', component: () => <Placeholder title="Tickets" /> })
const ticketRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'ticket/$key',
  component: function TicketRoute() {
    const { key } = ticketRoute.useParams()
    return <Placeholder title={key} />
  },
})
const agentsRoute = createRoute({ getParentRoute: () => rootRoute, path: 'agents', component: () => <Placeholder title="Agents" /> })
const settingsRoute = createRoute({ getParentRoute: () => rootRoute, path: 'settings', component: () => <Placeholder title="Settings" /> })
const addonRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'addon/$name/$id',
  component: function AddonRoute() {
    const { name, id } = addonRoute.useParams()
    return <Placeholder title={id} addon={name} />
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
