import { useRouterState } from '@tanstack/react-router'
import { useQueryClient } from '@tanstack/react-query'
import { Plus, RotateCcw, Search } from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useShellActions, useShellState } from './ShellUi'
import { toastApiError } from '@/app/toast'

const TITLES: Record<string, string> = {
  '/': 'Today',
  '/board': 'Board',
  '/tickets': 'Tickets',
  '/agents': 'Agents',
  '/settings': 'Settings',
}

const isMac = typeof navigator !== 'undefined' && /mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent)

export function Topbar() {
  const { header } = useShellState()
  const { openPalette, openNewTicket } = useShellActions()
  const pathname = useRouterState({ select: (s) => s.location.pathname })
  const qc = useQueryClient()

  const fallback = TITLES[pathname] ?? (pathname.startsWith('/ticket/') ? pathname.slice('/ticket/'.length) : '')
  const reset = async () => {
    try {
      await api.resetDemo()
      await qc.invalidateQueries()
      toast.success('Demo data reset')
    } catch (e) {
      toastApiError(e, 'Reset failed')
    }
  }

  return (
    <header className="flex h-12 shrink-0 items-center gap-3 border-b border-border bg-bg px-4">
      <div className="flex min-w-0 items-center gap-2 text-[13px]">
        {header.breadcrumb}
        <span className="truncate font-medium text-text" data-testid="topbar-title">
          {header.title ?? fallback}
        </span>
      </div>
      <div className="flex-1" />
      <div className="flex items-center gap-1">
        <Badge variant="outline" className="gap-1 border-warning/40 pr-1 font-normal text-warning">
          Demo data
          <button
            type="button"
            onClick={reset}
            className="inline-flex items-center gap-1 rounded-sm px-1 text-[11px] text-text-muted hover:bg-surface-3 hover:text-text"
            aria-label="Reset demo"
          >
            <RotateCcw className="size-3" />
            Reset demo
          </button>
        </Badge>
      </div>
      <button
        type="button"
        onClick={openPalette}
        className="flex h-8 w-[300px] items-center gap-2 rounded-md border border-border bg-surface px-2.5 text-[13px] text-text-faint hover:border-border-strong hover:text-text-muted"
      >
        <Search className="size-3.5" />
        <span className="flex-1 text-left">Search or run a command</span>
        <kbd className="rounded border border-border bg-surface-2 px-1.5 font-mono text-[10px] text-text-muted">{isMac ? '⌘K' : 'Ctrl K'}</kbd>
      </button>
      <Button size="sm" onClick={openNewTicket}>
        <Plus />
        New ticket
      </Button>
    </header>
  )
}
