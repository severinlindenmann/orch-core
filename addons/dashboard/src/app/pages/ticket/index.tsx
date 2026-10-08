import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { ChevronRight, Lock, TriangleAlert } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
import { workspaceOfTicket } from '@/api/workspaces'
import { useWorkspace } from '@/app/workspace'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { usePageHeader } from '../../shell/ShellUi'
import { AcceptanceTasks } from './AcceptanceTasks'
import { Artifacts } from './Artifacts'
import { GatesStrip } from './Gates'
import { TicketHeader } from './Header'
import { History } from './History'
import { Overview } from './Overview'
import { Questions } from './Questions'
import { Raw } from './Raw'
import { Rail } from './Rail'
import { SignDialog } from './SignDialog'
import { useViewer, type HumanAction, type Jump, type TabId, type TabProps } from './shared'

function TicketSkeleton() {
  return (
    <div className="space-y-4" aria-busy="true" aria-label="Loading ticket">
      <Skeleton className="h-4 w-40" />
      <Skeleton className="h-7 w-2/3" />
      <Skeleton className="h-6 w-1/2" />
      <Skeleton className="h-14 w-full" />
      <div className="flex gap-3">
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} className="h-28 flex-1" />
        ))}
      </div>
      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
        <Skeleton className="h-96" />
        <Skeleton className="h-96" />
      </div>
    </div>
  )
}

function NotVisible({ ticketKey }: { ticketKey: string }) {
  return (
    <div className="mx-auto mt-16 max-w-md rounded-lg border border-border bg-surface p-8 text-center" role="status">
      <Lock className="mx-auto size-7 text-text-muted" />
      <h1 className="mt-3 text-lg font-semibold">This ticket is not visible to you</h1>
      <p className="mt-1.5 text-[13px] text-text-muted">
        <span className="font-mono">{ticketKey}</span> is restricted to the people it lists. The host hides it from everyone else, so there is nothing to show and no way to tell who can see it.
      </p>
      <Button asChild variant="outline" size="sm" className="mt-4">
        <Link to="/board">Back to the board</Link>
      </Button>
    </div>
  )
}

function NotFound({ ticketKey, message, retry }: { ticketKey: string; message: string; retry?: () => void }) {
  return (
    <div className="mx-auto mt-16 max-w-md rounded-lg border border-border bg-surface p-8 text-center" role="alert">
      <TriangleAlert className="mx-auto size-7 text-warning" />
      <h1 className="mt-3 text-lg font-semibold">Could not load {ticketKey}</h1>
      <p className="mt-1.5 text-[13px] text-text-muted">{message}</p>
      {retry && (
        <Button variant="outline" size="sm" className="mt-4" onClick={retry}>
          Try again
        </Button>
      )}
    </div>
  )
}

/**
 * A ticket lives in one workspace. Opened from another one (a link, palette Recent), the page makes the ticket's
 * home the current workspace first (no "leaving" redirect), so every slot, addon state and action uses it.
 */
function useHomeWorkspace(ticketKey: string): { ready: boolean } {
  const { workspace, workspaces, setWorkspaceId } = useWorkspace()
  const home = workspaceOfTicket(ticketKey, workspaces)
  useEffect(() => {
    if (home && home.id !== workspace?.id) setWorkspaceId(home.id)
    // Only when the ticket (or its home) changes: a later switch away is the user's, and leaves the page.
  }, [ticketKey, home?.id])
  return { ready: !home || home.id === workspace?.id }
}

export function TicketPage({ ticketKey }: { ticketKey: string }) {
  const home = useHomeWorkspace(ticketKey)
  const viewer = useViewer(ticketKey)
  const q = useQuery({
    queryKey: ['ticket', ticketKey],
    queryFn: () => api.getTicket(ticketKey),
    retry: false,
  })
  const [tab, setTab] = useState<TabId>('overview')
  const [focus, setFocus] = useState<string | undefined>()
  const [signing, setSigning] = useState<HumanAction | null>(null)

  const breadcrumb = useMemo(
    () => (
      <>
        <Link to="/board" className="text-text-muted hover:text-text">
          Board
        </Link>
        <ChevronRight className="size-3.5 text-text-faint" aria-hidden />
      </>
    ),
    [],
  )
  usePageHeader(ticketKey, breadcrumb)

  useEffect(() => {
    setTab('overview')
    setFocus(undefined)
  }, [ticketKey])

  const jump = useCallback((j: Jump) => {
    setTab(j.tab)
    setFocus(j.id)
  }, [])

  useEffect(() => {
    if (!focus) return
    const t = setTimeout(() => {
      const el = document.getElementById(focus)
      el?.scrollIntoView?.({ block: 'center', behavior: 'smooth' })
      el?.classList.add('ring-2', 'ring-brand', 'rounded-md')
      setTimeout(() => el?.classList.remove('ring-2', 'ring-brand'), 1600)
    }, 60)
    return () => clearTimeout(t)
  }, [focus, tab])

  if (q.isLoading || !viewer.ready || !home.ready) return <TicketSkeleton />
  if (q.error) {
    if (q.error instanceof ApiError && q.error.code === 'not_visible') return <NotVisible ticketKey={ticketKey} />
    if (q.error instanceof ApiError && q.error.status === 404) return <NotFound ticketKey={ticketKey} message="There is no ticket with this key in your workspaces." />
    return <NotFound ticketKey={ticketKey} message={q.error instanceof Error ? q.error.message : 'Something went wrong.'} retry={() => void q.refetch()} />
  }
  const ticket = q.data
  if (!ticket) return <TicketSkeleton />

  const props: TabProps = { ticket, viewer, jump, sign: setSigning }
  const openQuestions = ticket.questions_state.filter((x) => x.state === 'open').length
  const provenCount = ticket.acceptance_state.filter((a) => a.state === 'proven').length

  return (
    <div className="mx-auto max-w-[1280px] space-y-5 pb-12">
      <TicketHeader ticket={ticket} viewer={viewer} sign={setSigning} jump={jump} />
      <GatesStrip ticket={ticket} viewer={viewer} />

      <div className="grid items-start gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
        <Tabs
          value={tab}
          onValueChange={(v) => {
            setTab(v as TabId)
            setFocus(undefined)
          }}
          className="min-w-0 gap-4"
        >
          <TabsList variant="line" className="h-9 w-full justify-start gap-1 border-b border-border">
            <TabsTrigger value="overview">Overview</TabsTrigger>
            <TabsTrigger value="acceptance">
              Acceptance &amp; tasks
              <span className="font-mono text-[11px] text-text-faint">
                {provenCount}/{ticket.acceptance_state.length}
              </span>
            </TabsTrigger>
            <TabsTrigger value="questions">
              Questions
              {openQuestions > 0 && <span className="rounded-full bg-warning-soft px-1.5 font-mono text-[11px] text-warning">{openQuestions}</span>}
            </TabsTrigger>
            <TabsTrigger value="artifacts">
              Artifacts
              <span className="font-mono text-[11px] text-text-faint">{ticket.artifacts.length}</span>
            </TabsTrigger>
            <TabsTrigger value="history">History</TabsTrigger>
            <TabsTrigger value="raw">Raw</TabsTrigger>
          </TabsList>
          <TabsContent value="overview">
            <Overview {...props} />
          </TabsContent>
          <TabsContent value="acceptance">
            <AcceptanceTasks {...props} />
          </TabsContent>
          <TabsContent value="questions">
            <Questions {...props} />
          </TabsContent>
          <TabsContent value="artifacts">
            <Artifacts {...props} focus={focus} />
          </TabsContent>
          <TabsContent value="history">
            <History {...props} />
          </TabsContent>
          <TabsContent value="raw">
            <Raw {...props} />
          </TabsContent>
        </Tabs>
        <Rail ticket={ticket} viewer={viewer} sign={setSigning} jump={jump} />
      </div>

      <SignDialog ticket={ticket} action={signing} onClose={() => setSigning(null)} />
    </div>
  )
}
