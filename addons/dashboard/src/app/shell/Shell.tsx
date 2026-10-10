import { useEffect, useRef } from 'react'
import { Outlet, useRouterState } from '@tanstack/react-router'
import { ErrorBoundary, PageProblem } from '@/components/ErrorBoundary'
import { TooltipProvider } from '@/components/ui/tooltip'
import { retryFailedPageLoads } from '../pages/lazyPage'
import { GenericSkeleton } from '../pages/skeletons'
import { PageFade, usePageScroll } from './pageMotion'
import { DockArea } from '../terminal/dock/DockArea'
import { useLiveUpdates } from '../live'
import { useWorkspace, WorkspaceProvider } from '../workspace'
import { WorkspaceNotFound } from './WorkspaceNotFound'
import { HelpSheet } from './HelpSheet'
import { NewTicketHost } from './NewTicketHost'
import { CommandPalette } from './palette'
import { ShellUiProvider } from './ShellUi'
import { useShortcuts } from './shortcuts'
import { ShellToaster } from './ShellToaster'
import { Sidebar } from './Sidebar'
import { Topbar } from './Topbar'

/** Keyboard shortcuts from `shortcuts.ts`. */
function Shortcuts() {
  useShortcuts()
  return null
}

/** The addon renderers (widgets, forms, markdown, charts, terminal) load once the app is idle: ticket panels and
 * addon blocks then render at once, without a placeholder of another size. */
function PreloadAddonNodes() {
  useEffect(() => {
    if (import.meta.env.MODE === 'test') return
    const go = () => void import('@/addon-ui/preloadNodes').then((m) => m.preloadAddonNodes())
    const w = window as Window & { requestIdleCallback?: (cb: () => void, o?: { timeout: number }) => number; cancelIdleCallback?: (id: number) => void }
    if (w.requestIdleCallback) {
      const id = w.requestIdleCallback(go, { timeout: 3000 })
      return () => w.cancelIdleCallback?.(id)
    }
    const t = setTimeout(go, 1500)
    return () => clearTimeout(t)
  }, [])
  return null
}

function LiveUpdates() {
  useLiveUpdates()
  return null
}

/** The page, or "no such workspace" when the address names a workspace the viewer does not have (a skeleton until known). */
function PageOutlet() {
  const { missingPrefix, pendingPrefix } = useWorkspace()
  if (pendingPrefix) return <GenericSkeleton />
  return missingPrefix !== undefined ? <WorkspaceNotFound prefix={missingPrefix} /> : <Outlet />
}

export function Shell() {
  const path = useRouterState({ select: (s) => s.resolvedLocation?.href ?? s.location.href })
  const main = useRef<HTMLElement>(null)
  usePageScroll(main)
  return (
    <WorkspaceProvider>
      <LiveUpdates />
      <PreloadAddonNodes />
      <ShellUiProvider>
        <Shortcuts />
        <TooltipProvider delayDuration={250}>
          <a
            href="#main"
            onClick={(e) => {
              // A hash link would become a router navigation: move the focus by hand.
              e.preventDefault()
              document.getElementById('main')?.focus()
            }}
            className="sr-only z-50 rounded-md bg-surface px-3 py-2 text-[13px] text-text shadow-lg focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:ring-2 focus:ring-brand"
          >
            Skip to content
          </a>
          <div className="flex h-full min-w-[1024px]">
            <Sidebar />
            <div className="flex min-w-0 flex-1 flex-col">
              <Topbar />
              <DockArea>
              <main ref={main} id="main" tabIndex={-1} className="min-h-0 flex-1 overflow-y-auto p-4 outline-none @[60rem]/page:p-6">
                <ErrorBoundary resetKey={path} fallback={(retry) => (
                    <PageProblem
                      retry={() => {
                        retryFailedPageLoads()
                        retry()
                      }}
                    />
                  )}>
                  {/* The router shows each page's skeleton while it loads (router.tsx): no Suspense fallback here. */}
                  <PageFade>
                    <PageOutlet />
                  </PageFade>
                </ErrorBoundary>
              </main>
              </DockArea>
            </div>
          </div>
          <CommandPalette />
          <NewTicketHost />
          <HelpSheet />
          <ShellToaster />
        </TooltipProvider>
      </ShellUiProvider>
    </WorkspaceProvider>
  )
}
