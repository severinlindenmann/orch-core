import type { QueryClient } from '@tanstack/react-query'
import { createBrowserHistory, createMemoryHistory, createRootRouteWithContext, createRoute, createRouter, Outlet, redirect, useRouterState } from '@tanstack/react-router'
import { Shell } from './shell/Shell'
import { RouteProblem } from './shell/RouteProblem'
import { lazyPage } from './pages/lazyPage'
import { AddonPageSkeleton, AgentsSkeleton, ArtifactsSkeleton, BoardSkeleton, GenericSkeleton, SettingsSkeleton, TicketSkeleton, TicketsSkeleton, TodaySkeleton } from './pages/skeletons'
import { knownSettingsTab } from './pages/settings/tabs'
import { validateTicketsSearch } from './pages/tickets/search'
import { addonPageData, agentsData, artifactsData, boardData, loadShell, pageLoader, settingsData, ticketData, ticketsData, todayData, type LoaderContext } from './routeData'
import { validateAgentsSearch, validateArtifactsSearch, validateBoardSearch, validateTicketSearch } from './search'
import { workspaceRewrite, type UrlState } from './urls'
import { hasFeature, isRealHost } from '@/api/features'
import { queries } from '@/api/queries'

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
const QuickPage = lazyPage(() => import('./pages/quick'), 'QuickPage')
const DropPage = lazyPage(() => import('./pages/drop'), 'DropPage')
// The tester's checklist: its own page without the shell, so it works before any workspace exists (orch setup).
const ChecklistPage = lazyPage(() => import('./checklist/ChecklistPage'), 'ChecklistPage')
// The feedback page: also its own page without the shell (saved in the browser, asks the host for nothing).
const FeedbackPage = lazyPage(() => import('./feedback/FeedbackPage'), 'FeedbackPage')
const GuidePage = lazyPage(() => import('./pages/guide'), 'GuidePage')
const MarketPage = lazyPage(() => import('./pages/market'), 'MarketPage')
const SharesPage = lazyPage(() => import('./pages/shares'), 'SharesPage')
const WorktreesPage = lazyPage(() => import('./pages/worktrees'), 'WorktreesPage')
const ExplorerPage = lazyPage(() => import('./pages/explorer'), 'ExplorerPage')
const TerminalsPage = lazyPage(() => import('./pages/terminals'), 'TerminalsPage')
const ActivityPage = lazyPage(() => import('./pages/activity'), 'ActivityPage')
const FocusPage = lazyPage(() => import('./pages/focus'), 'FocusPage')
const SchedulesPage = lazyPage(() => import('./pages/schedules'), 'SchedulesPage')
const UsagePage = lazyPage(() => import('./pages/usage'), 'UsagePage')
// Preview component is dev only; the production route remains registered as a redirect to /.
// The real-host-only pages (quick tasks, records, runner) drawn from fixtures so the layout guard sees them (src/dev).
// `import.meta.env.DEV` is false in a build, so the import, its chunk and the fixtures are not in the production bundle.
const LayoutPreview = import.meta.env.DEV ? lazyPage(() => import('../dev/layoutPreview'), 'LayoutPreview') : () => null
const AddonPage = lazyPage(() => import('./pages/AddonPage'), 'AddonPage')
/** The addon node renderers an addon page or a ticket's panels usually need (widgets, forms, markdown, charts). */
const addonNodes = () => import('@/addon-ui/preloadNodes').then((m) => m.preloadAddonNodes())

/** Under this a page simply replaces the old one; past it the page's skeleton shows, for at least PENDING_MIN_MS. */
export const PENDING_MS = 120
export const PENDING_MIN_MS = 300

/** The tester's checklist (not in the nav; opened with `orch dashboard open /checklist`). */
export const CHECKLIST_PATH = '/checklist'
/** The feedback page (sidebar link under the test checklist). */
export const FEEDBACK_PATH = '/feedback'
const isBarePage = (path: string) => path === CHECKLIST_PATH || path === FEEDBACK_PATH

/** A page this build or host does not serve (the read-only production app) goes to the board; the mock serves all. */
const serves = (feature: string) => () => {
  if (!hasFeature(feature)) throw redirect({ to: hasFeature('board') ? '/board' : '/' })
}

// Code-based route tree with short in-app paths. The address bar carries the workspace (`/w/DEMO/board`): the router's
// rewrite (urls.ts) strips it on the way in and adds the current one on the way out. Browser history (real paths) in
// the app; memory history only where a test passes an initial path.
export interface RouterContext extends LoaderContext {
  urls: UrlState
}
// The shell's data loads before its first paint (nothing to keep on screen yet: the document's own background).
function Root() {
  const path = useRouterState({ select: (s) => s.location.pathname })
  return isBarePage(path) ? <Outlet /> : <Shell />
}
const rootRoute = createRootRouteWithContext<RouterContext>()({
  component: Root,
  // The checklist and feedback open before any workspace exists: they ask the host for nothing (no 503s in the console).
  loader: ({ context, location }) => (isBarePage(location.pathname) ? undefined : loadShell(context)),
  pendingComponent: () => null,
  pendingMinMs: 0,
})

const todayRoute = createRoute({ getParentRoute: () => rootRoute, path: '/', component: TodayPage, loader: pageLoader(todayData, [TodayPage.preload]), pendingComponent: () => <TodaySkeleton /> })
const boardRoute = createRoute({ getParentRoute: () => rootRoute, path: 'board', beforeLoad: serves('board'), validateSearch: validateBoardSearch, component: BoardPage, loader: pageLoader(boardData, [BoardPage.preload]), pendingComponent: BoardSkeleton })
const ticketsRoute = createRoute({
  getParentRoute: () => rootRoute, path: 'tickets', beforeLoad: serves('tickets'),
  validateSearch: validateTicketsSearch,
  component: TicketsPage,
  // The search is read once, on entering the page: a filter change keeps the page and refetches in place.
  loader: ({ context, location }) => pageLoader(ticketsData(validateTicketsSearch(location.search as Record<string, unknown>)), [TicketsPage.preload])({ context }),
  pendingComponent: TicketsSkeleton,
})
const newTicketRoute = createRoute({ getParentRoute: () => rootRoute, path: 'tickets/new', beforeLoad: serves('actions'), component: NewTicketPage, loader: pageLoader(() => [], [NewTicketPage.preload]) })
const ticketRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'ticket/$key',
  beforeLoad: serves('ticket'),
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
  beforeLoad: serves('artifacts'),
  validateSearch: validateArtifactsSearch,
  component: ArtifactsPage,
  loader: pageLoader(artifactsData, [ArtifactsPage.preload]),
  pendingComponent: function ArtifactsPending() {
    // The layout the address asks for (the viewer's remembered one is not known yet: list).
    const { view } = artifactsRoute.useSearch()
    return <ArtifactsSkeleton view={view ?? 'list'} />
  },
})
/** Quick tasks of a real host (M6): the mock keeps its own addon page, so this route is for a real host that serves `quick/1`. */
const quickRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'quick',
  beforeLoad: () => {
    if (!isRealHost()) throw redirect({ to: '/' })
    serves('quick')()
  },
  loader: pageLoader(() => [], [QuickPage.preload]),
  component: QuickPage,
})
/**
 * Drop of a real host (proposed `drop/1`): the mock keeps its own Drop addon page. On a real host the route is always
 * there; without the gate the page says "Drop is not available on this host yet." and calls nothing.
 */
const dropRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'drop',
  beforeLoad: () => {
    if (!isRealHost()) throw redirect({ to: '/' })
  },
  loader: pageLoader(() => [], [DropPage.preload]),
  component: DropPage,
})
const feedbackRoute = createRoute({ getParentRoute: () => rootRoute, path: 'feedback', loader: pageLoader(() => [], [FeedbackPage.preload]), component: FeedbackPage })
const checklistRoute = createRoute({ getParentRoute: () => rootRoute, path: 'checklist', loader: pageLoader(() => [], [ChecklistPage.preload]), component: ChecklistPage })
/** The guide of a real host (fixed pages in the bundle). The mock keeps its guide addon page. */
const guideRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'guide',
  validateSearch: (s: Record<string, unknown>): { page?: string } => (typeof s.page === 'string' ? { page: s.page } : {}),
  beforeLoad: () => {
    if (!isRealHost()) throw redirect({ to: '/' })
  },
  loader: pageLoader(() => [], [GuidePage.preload, addonNodes]),
  component: GuidePage,
})
/** Work copies of a real host (H11d): the mock keeps its own addon page; a host without `worktree/1` shows a plain note. */
const worktreesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'worktrees',
  beforeLoad: () => {
    if (!isRealHost()) throw redirect({ to: '/' })
    serves('worktrees')()
  },
  loader: pageLoader(() => [], [WorktreesPage.preload]),
  component: WorktreesPage,
})
/** Terminals of a real host (add-on): listed only while the add-on is ON (the server adds `terminals_page` then). */
const terminalsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'terminals',
  beforeLoad: () => {
    if (!isRealHost()) throw redirect({ to: '/' })
    serves('terminals_page')()
  },
  loader: pageLoader(() => [], [TerminalsPage.preload]),
  component: TerminalsPage,
})
/** File Explorer of a real host (add-on, owners only): listed only while the add-on is ON; the server refuses anyone else. */
const explorerRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'explorer',
  validateSearch: (s: Record<string, unknown>): { root?: string; path?: string; rev?: string; view?: 'history'; sha?: string } => ({
    ...(typeof s.root === 'string' ? { root: s.root } : {}),
    ...(typeof s.path === 'string' ? { path: s.path } : {}),
    ...(typeof s.rev === 'string' ? { rev: s.rev } : {}),
    ...(s.view === 'history' ? { view: 'history' as const } : {}),
    ...(typeof s.sha === 'string' ? { sha: s.sha } : {}),
  }),
  beforeLoad: () => {
    if (!isRealHost()) throw redirect({ to: '/' })
    serves('explorer')()
  },
  loader: pageLoader(() => [], [ExplorerPage.preload]),
  component: ExplorerPage,
})
/** Owner marketplace, served only by a real host advertising market. */
const marketRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'addons',
  validateSearch: (s: Record<string, unknown>): { tab: 'browse' | 'installed' | 'marketplaces' } => ({ tab: s.tab === 'installed' || s.tab === 'marketplaces' ? s.tab : 'browse' }),
  beforeLoad: () => {
    if (!isRealHost()) throw redirect({ to: '/' })
    // The page also serves a host without the market ops: it then shows the built-in add-ons only.
    serves('settings')()
  },
  loader: pageLoader(({ qc, ws }) => hasFeature('market', ws.id) ? [qc.ensureQueryData(queries.marketList(ws.id))] : [], [MarketPage.preload]),
  component: MarketPage,
})
/** Shares of a real host (Publish add-on): listed only while the add-on is ON. */
const sharesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'shares',
  beforeLoad: () => {
    if (!isRealHost()) throw redirect({ to: '/' })
    serves('publish')()
  },
  loader: pageLoader(() => [], [SharesPage.preload]),
  component: SharesPage,
})
/** Activity of a real host (#595): the mock keeps its own addon page; a real host that serves `activity/1` gets this route. */
const activityRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'activity',
  beforeLoad: () => {
    if (!isRealHost()) throw redirect({ to: '/' })
  },
  loader: pageLoader(() => [], [ActivityPage.preload]),
  component: ActivityPage,
})
/** Usage of a real host (add-on): listed only while the add-on is ON for this workspace (the server adds `usage` then). */
const usageRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'usage',
  beforeLoad: () => {
    if (!isRealHost()) throw redirect({ to: '/' })
    serves('usage')()
  },
  loader: pageLoader(() => [], [UsagePage.preload]),
  component: UsagePage,
})
/** Schedules of a real host (add-on): the follow-ups agents set; listed only while the add-on is ON (the server adds `schedules` then). */
const schedulesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'schedules',
  beforeLoad: () => {
    if (!isRealHost()) throw redirect({ to: '/' })
    serves('schedules')()
  },
  loader: pageLoader(() => [], [SchedulesPage.preload]),
  component: SchedulesPage,
})
/** The old address of the Wiki: kept as a plain redirect to the add-on's page. */
const wikiRedirectRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'wiki',
  beforeLoad: () => {
    throw redirect({ to: '/addon/$name/$page', params: { name: 'wiki', page: 'wiki' } })
  },
})
const focusRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'focus',
  validateSearch: (s: Record<string, unknown>): { scratch?: 1 | string } =>
    Number(s.scratch) === 1 ? { scratch: 1 } : typeof s.scratch === 'string' && /^[A-Za-z0-9_-]{2,64}$/.test(s.scratch) ? { scratch: s.scratch } : {},
  beforeLoad: async ({ context }) => {
    await context.queryClient?.ensureQueryData(queries.workspaces())
    serves('focus')()
  },
  loader: pageLoader(() => [], [FocusPage.preload]),
  component: FocusPage,
})
const focusTicketRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'focus/$ticket',
  beforeLoad: async ({ context }) => {
    await context.queryClient?.ensureQueryData(queries.workspaces())
    serves('focus')()
  },
  loader: pageLoader(() => [], [FocusPage.preload]),
  component: FocusPage,
})
const layoutPreviewRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '__layout/$name',
  beforeLoad: () => {
    if (!import.meta.env.DEV) throw redirect({ to: '/' })
  },
  component: function LayoutPreviewRoute() {
    const { name } = layoutPreviewRoute.useParams()
    return <LayoutPreview key={name} name={name} />
  },
})
const agentsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'agents',
  beforeLoad: serves('agents'),
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
    serves('settings')()
    throw redirect({ to: '/settings/$tab', params: { tab: 'general' } })
  },
})
const settingsTabRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: 'settings/$tab',
  // An unknown tab (or /settings/addon without a name) would render only the sub-nav: go to General instead.
  beforeLoad: ({ params }) => {
    serves('settings')()
    // Add-ons live on the Add-ons page now (the old Settings tab is gone).
    if (isRealHost() && params.tab === 'addons') throw redirect({ to: '/addons', search: { tab: 'installed' } })
    if (!knownSettingsTab(params.tab)) throw redirect({ to: '/settings/$tab', params: { tab: 'general' } })
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
  beforeLoad: () => {
    serves('settings')()
    // An addon's own settings are not on a real host yet (the reduced settings have General, Members, Gates, Grants).
    if (isRealHost()) throw redirect({ to: '/addons', search: { tab: 'installed' } })
  },
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
  beforeLoad: serves('addons'),
  loader: ({ context, params }) => pageLoader(addonPageData(params.name), [AddonPage.preload, addonNodes])({ context }),
  pendingComponent: () => <AddonPageSkeleton />,
  component: function AddonRoute() {
    const { name, page } = addonRoute.useParams()
    return <AddonPage name={name} page={page} />
  },
})

const routeTree = rootRoute.addChildren([todayRoute, boardRoute, ticketsRoute, newTicketRoute, ticketRoute, artifactsRoute, agentsRoute, quickRoute, dropRoute, checklistRoute, feedbackRoute, worktreesRoute, terminalsRoute, explorerRoute, marketRoute, sharesRoute, activityRoute, usageRoute, schedulesRoute, wikiRedirectRoute, focusRoute, focusTicketRoute, guideRoute, layoutPreviewRoute, settingsRoute, settingsTabRoute, settingsAddonRoute, settingsAddonsAliasRoute, addonRoute])

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
