import { Outlet } from '@tanstack/react-router'
import { Toaster } from '@/components/ui/sonner'
import { TooltipProvider } from '@/components/ui/tooltip'
import { WorkspaceProvider } from '../workspace'
import { CommandPalette } from './CommandPalette'
import { NewTicketDialog } from './NewTicketDialog'
import { ShellUiProvider } from './ShellUi'
import { Sidebar } from './Sidebar'
import { Topbar } from './Topbar'

export function Shell() {
  return (
    <WorkspaceProvider>
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
          <NewTicketDialog />
          <Toaster position="bottom-right" />
        </TooltipProvider>
      </ShellUiProvider>
    </WorkspaceProvider>
  )
}
