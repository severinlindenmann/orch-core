import { useQuery } from '@tanstack/react-query'
import { Link, useRouter, useRouterState } from '@tanstack/react-router'
import { ChevronRight, Lock, TriangleAlert } from 'lucide-react'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
import { workspaceOfTicket } from '@/api/workspaces'
import { useWorkspace } from '@/app/workspace'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { usePageHeader, useTicketOrigin } from '../../shell/ShellUi'
import { AcceptanceTasks } from './AcceptanceTasks'
import { Artifacts } from './Artifacts'
import { Changes, HAS_CHANGES } from './Changes'
import { diffstat } from '@/api/gates'
import { GatesStrip } from './Gates'
import { TicketHeader } from './Header'
import { History } from './History'
import { Overview } from './Overview'
import { Questions } from './Questions'
import { Raw } from './Raw'
import { PropertiesStrip, Rail } from './Rail'
import { SignDialog } from './SignDialog'
import { useViewer, useWideLayout, type HumanAction, type Jump, type TabId, type TabProps } from './shared'

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
      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
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
    if (home && home.id !== workspace?.id) {
      setWorkspaceId(home.id)
      toast(`Switched to ${home.name} to open ${ticketKey}`)
    }
    // Only when the ticket (or its home) changes: a later switch away is the user's, and leaves the page.
  }, [ticketKey, home?.id])
  return { ready: !home || home.id === workspace?.id }
}

const questionOf = (hash: string) => (/^question-[A-Za-z0-9_-]+$/.test(hash) ? hash : undefined)

export function TicketPage({ ticketKey }: { ticketKey: string }) {
  const hash = useRouterState({ select: s => s.location.hash })
  const home = useHomeWorkspace(ticketKey)
  const viewer = useViewer(ticketKey)
  const q = useQuery({
    queryKey: ['ticket', ticketKey],
    queryFn: () => api.getTicket(ticketKey),
    retry: false,
  })
  // A `#question-Q2` link (Today's Agents panel) opens the Questions tab on that question from the first paint.
  const [tab, setTab] = useState<TabId>(() => (questionOf(hash) ? 'questions' : 'overview'))
  const [focus, setFocus] = useState<string | undefined>(() => questionOf(hash))
  const applied = useRef(`${ticketKey}#${hash}`)
  const [signing, setSigning] = useState<HumanAction | null>(null)
  // From the touch until the host confirms, the header's actions say "Signing…" and are off (R-d).
  const [signPending, setSignPending] = useState(false)
  const wide = useWideLayout()

  // Back to where the ticket was opened from (Today, Board, Tickets, Artifacts, an addon page), filters included.
  const origin = useTicketOrigin()
  const router = useRouter()
  const breadcrumb = useMemo(
    () => (
      <>
        <a
          href={origin.href}
          onClick={(e) => {
            if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return
            e.preventDefault()
            router.history.push(origin.href)
          }}
          className="text-text-muted hover:text-text"
        >
          {origin.label}
        </a>
        <ChevronRight className="size-3.5 text-text-faint" aria-hidden />
      </>
    ),
    [origin, router],
  )
  usePageHeader(ticketKey, breadcrumb)

  // Another ticket or another hash on the same page: choose the tab again, before paint.
  useLayoutEffect(() => {
    const now = `${ticketKey}#${hash}`
    if (applied.current === now) return
    applied.current = now
    const question = questionOf(hash)
    setTab(question ? 'questions' : 'overview')
    setFocus(question)
  }, [ticketKey, hash])

  const jump = useCallback((j: Jump) => {
    setTab(j.tab)
    setFocus(j.id)
  }, [])

  // Runs again once the ticket is on screen, so a cold deep link highlights its target too.
  const shown = !!q.data && viewer.ready && home.ready
  useEffect(() => {
    if (!focus || !shown) return
    const t = setTimeout(() => {
      const el = document.getElementById(focus)
      el?.scrollIntoView?.({ block: 'center', behavior: 'smooth' })
      el?.classList.add('ring-2', 'ring-brand', 'rounded-md')
      setTimeout(() => el?.classList.remove('ring-2', 'ring-brand'), 1600)
    }, 60)
    return () => clearTimeout(t)
  }, [focus, tab, shown])

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
    <div className="mx-auto min-w-0 max-w-[1280px] space-y-4 pb-12">
      <TicketHeader ticket={ticket} viewer={viewer} sign={setSigning} jump={jump} signing={signPending} />
      <GatesStrip ticket={ticket} viewer={viewer} />
      {!wide && <PropertiesStrip ticket={ticket} viewer={viewer} />}

      {/* From 1280 px the rail is a 320 px column; below, it is the Panels sheet (it never drops under the content). */}
      <div className={wide ? 'grid min-w-0 grid-cols-[minmax(0,1fr)_320px] items-start gap-6' : 'min-w-0'}>
        <Tabs
          value={tab}
          onValueChange={(v) => {
            setTab(v as TabId)
            setFocus(undefined)
          }}
          className="min-w-0 gap-4"
        >
          <TabsList variant="line" className="h-9 w-full min-w-0 justify-start gap-1 overflow-x-auto overflow-y-hidden border-b border-border [&>*]:flex-none" data-scroll-tabs>
            <TabsTrigger value="overview">Overview</TabsTrigger>
            <TabsTrigger value="acceptance">
              Acceptance &amp; tasks
              <span className="font-mono text-[11px] text-text-faint">
                {provenCount}/{ticket.acceptance_state.length}
              </span>
            </TabsTrigger>
            {HAS_CHANGES.has(ticket.status) && (
              <TabsTrigger value="changes">
                Changes
                <span className="font-mono text-[11px] text-text-faint">{diffstat(ticket.branch)}</span>
              </TabsTrigger>
            )}
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
          {HAS_CHANGES.has(ticket.status) && (
            <TabsContent value="changes">
              <Changes {...props} />
            </TabsContent>
          )}
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
        {wide && <Rail ticket={ticket} viewer={viewer} />}
      </div>

      <SignDialog ticket={ticket} action={signing} onClose={() => setSigning(null)} onOpenEvidence={() => jump({ tab: 'acceptance' })} onOpenChanges={() => jump({ tab: 'changes' })} onPending={setSignPending} />
    </div>
  )
}
