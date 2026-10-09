import { Suspense } from 'react'
import { Outlet, useRouterState } from '@tanstack/react-router'
import { ErrorBoundary, PageProblem } from '@/components/ErrorBoundary'
import { Skeleton } from '@/components/ui/skeleton'
import { TooltipProvider } from '@/components/ui/tooltip'
import { retryFailedPageLoads } from '../pages/lazyPage'
import { DockArea } from '../terminal/dock/DockArea'
import { useLiveUpdates } from '../live'
import { WorkspaceProvider } from '../workspace'
import { HelpSheet } from './HelpSheet'
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

function LiveUpdates() {
  useLiveUpdates()
  return null
}

/** Shown while a lazily loaded page chunk arrives. */
function PageSkeleton() {
  return (
    <div className="space-y-4" role="status" aria-label="Loading page">
      <Skeleton className="h-7 w-48" />
      <Skeleton className="h-40 w-full" />
    </div>
  )
}

export function Shell() {
  const path = useRouterState({ select: (s) => s.resolvedLocation?.href ?? s.location.href })
  return (
    <WorkspaceProvider>
      <LiveUpdates />
      <ShellUiProvider>
        <Shortcuts />
        <TooltipProvider delayDuration={250}>
          <a
            href="#main"
            onClick={(e) => {
              // Memory history: a hash link would navigate nowhere, so move the focus by hand.
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
              <main id="main" tabIndex={-1} className="min-h-0 flex-1 overflow-y-auto p-6 outline-none">
                <ErrorBoundary resetKey={path} fallback={(retry) => (
                    <PageProblem
                      retry={() => {
                        retryFailedPageLoads()
                        retry()
                      }}
                    />
                  )}>
                  <Suspense fallback={<PageSkeleton />}>
                    <Outlet />
                  </Suspense>
                </ErrorBoundary>
              </main>
              </DockArea>
            </div>
          </div>
          <CommandPalette />
          <HelpSheet />
          <ShellToaster />
        </TooltipProvider>
      </ShellUiProvider>
    </WorkspaceProvider>
  )
}
