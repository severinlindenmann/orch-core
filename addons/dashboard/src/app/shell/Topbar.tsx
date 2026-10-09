import { useRouterState } from '@tanstack/react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Plus, RotateCcw, Search } from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { can } from '@/api/permissions'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'
import { useRole } from '../useRole'
import { useShellActions, useShellState } from './ShellUi'
import { toastApiError } from '@/app/toast'

const TITLES: Record<string, string> = {
  '/': 'Today',
  '/board': 'Board',
  '/tickets': 'Tickets',
  '/artifacts': 'Artifacts',
  '/agents': 'Agents',
  '/settings': 'Settings',
}

type Dataset = 'normal' | 'busy'
const DATASET_LABEL: Record<Dataset, string> = { normal: 'Normal', busy: 'Busy day' }
const DATASET_NAME: Record<Dataset, string> = { normal: 'normal', busy: 'busy day' }

const isMac = typeof navigator !== 'undefined' && /mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent)

export function Topbar() {
  const { header } = useShellState()
  const { openPalette, openNewTicket } = useShellActions()
  const pathname = useRouterState({ select: (s) => s.location.pathname })
  const qc = useQueryClient()
  const dataset = useQuery({ queryKey: ['dev-dataset'], queryFn: () => api.getDataset() })
  const mode: Dataset = dataset.data?.dataset ?? 'normal'
  const [pending, setPending] = useState<Dataset | 'reset' | null>(null)
  const role = useRole()

  const fallback = TITLES[pathname] ?? (pathname.startsWith('/ticket/') ? pathname.slice('/ticket/'.length) : '')
  const reset = async () => {
    setPending(null)
    try {
      await api.resetDemo()
      await qc.invalidateQueries()
      for (const key of Object.keys(localStorage)) if (key.startsWith('orch.today.seen.')) localStorage.removeItem(key)
      qc.setQueryData(['demo-reset'], (n: number = 0) => n + 1)
      toast.success('Demo data reset')
    } catch (e) {
      toastApiError(e, 'Reset failed')
    }
  }
  // Switching resets the demo to that dataset (like Reset), so it is confirmed first.
  const switchTo = async (to: Dataset) => {
    setPending(null)
    try {
      await api.resetDemo(to)
      await qc.invalidateQueries()
      for (const key of Object.keys(localStorage)) if (key.startsWith('orch.today.seen.')) localStorage.removeItem(key)
      qc.setQueryData(['demo-reset'], (n: number = 0) => n + 1)
      toast.success(`Demo data: ${DATASET_NAME[to]}`)
    } catch (e) {
      toastApiError(e, 'Switch failed')
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
        <Badge variant="outline" className="gap-1 border-border pr-1 font-normal text-text-muted">
          <span data-testid="demo-mode">{mode === 'busy' ? 'Demo data · Busy day' : 'Demo data'}</span>
          <span role="group" aria-label="Demo dataset" className="inline-flex overflow-hidden rounded-sm border border-border">
            {(['normal', 'busy'] as const).map((d) => (
              <button
                key={d}
                type="button"
                aria-pressed={mode === d}
                onClick={() => d !== mode && setPending(d)}
                className={cn('px-1.5 text-[11px]', mode === d ? 'bg-surface-3 text-text' : 'text-text-muted hover:bg-surface-3 hover:text-text')}
              >
                {DATASET_LABEL[d]}
              </button>
            ))}
          </span>
          <button
            type="button"
            onClick={() => setPending('reset')}
            className="inline-flex items-center gap-1 rounded-sm px-1 text-[11px] text-text-muted hover:bg-surface-3 hover:text-text"
            aria-label="Reset demo"
          >
            <RotateCcw className="size-3" />
            Reset demo
          </button>
        </Badge>
      </div>
      {pending && (
        <Dialog open onOpenChange={(o) => !o && setPending(null)}>
          <DialogContent role="alertdialog" className="max-w-sm gap-4 border-border bg-surface">
            <DialogHeader>
              <DialogTitle>{pending === 'reset' ? 'Reset the demo?' : `Switch to the ${DATASET_NAME[pending]} demo?`}</DialogTitle>
              <DialogDescription>Your demo changes are discarded.</DialogDescription>
            </DialogHeader>
            <DialogFooter className="gap-2">
              <Button variant="ghost" onClick={() => setPending(null)}>
                Cancel
              </Button>
              {pending === 'reset' ? <Button onClick={reset}>Reset demo data</Button> : <Button onClick={() => switchTo(pending)}>Switch to {DATASET_NAME[pending]}</Button>}
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
      <button
        type="button"
        onClick={openPalette}
        className="flex h-8 w-[300px] items-center gap-2 rounded-md border border-border bg-surface px-2.5 text-[13px] text-text-faint hover:border-border-strong hover:text-text-muted"
      >
        <Search className="size-3.5" />
        <span className="flex-1 text-left">Search or run a command</span>
        <kbd className="rounded border border-border bg-surface-2 px-1.5 font-mono text-[10px] text-text-muted">{isMac ? '⌘K' : 'Ctrl K'}</kbd>
      </button>
      {role && can(role, 'ticket.create') && (
        <Button size="sm" onClick={openNewTicket}>
          <Plus />
          New ticket
        </Button>
      )}
    </header>
  )
}
