import { Outlet } from '@tanstack/react-router'
import { Toaster } from '@/components/ui/sonner'
import { TooltipProvider } from '@/components/ui/tooltip'
import { useLiveUpdates } from '../live'
import { WorkspaceProvider } from '../workspace'
import { CommandPalette } from './palette'
import { ShellUiProvider } from './ShellUi'
import { useShortcuts } from './shortcuts'
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

export function Shell() {
  return (
    <WorkspaceProvider>
      <LiveUpdates />
      <Shortcuts />
      <ShellUiProvider>
        <TooltipProvider delayDuration={250}>
          <div className="flex h-full min-w-[1024px]">
            <Sidebar />
            <div className="flex min-w-0 flex-1 flex-col">
              <Topbar />
              <main className="min-h-0 flex-1 overflow-y-auto p-6">
                <Outlet />
              </main>
            </div>
          </div>
          <CommandPalette />
          <Toaster position="bottom-right" />
        </TooltipProvider>
      </ShellUiProvider>
    </WorkspaceProvider>
  )
}
