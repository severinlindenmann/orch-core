import { Outlet, useNavigate } from '@tanstack/react-router'
import { useEffect } from 'react'
import { Toaster } from '@/components/ui/sonner'
import { TooltipProvider } from '@/components/ui/tooltip'
import { useLiveUpdates } from '../live'
import { WorkspaceProvider } from '../workspace'
import { CommandPalette } from './CommandPalette'
import { ShellUiProvider } from './ShellUi'
import { Sidebar } from './Sidebar'
import { Topbar } from './Topbar'

/** `c` opens the new-ticket page, unless the person is typing or a modifier is held. */
function NewTicketShortcut() {
  const navigate = useNavigate()
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'c' || e.metaKey || e.ctrlKey || e.altKey || e.shiftKey || e.defaultPrevented) return
      const t = e.target as HTMLElement | null
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.closest?.('[role="dialog"],[role="menu"],[role="listbox"]'))) return
      e.preventDefault()
      void navigate({ to: '/tickets/new' })
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [navigate])
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
      <NewTicketShortcut />
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
