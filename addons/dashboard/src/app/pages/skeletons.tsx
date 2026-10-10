// One skeleton per page (G4), shaped like the page it stands for: the same heading, the same columns and the same
// block heights, so the page replaces it without anything moving. The router shows it (as the route's pending
// component) only when a page takes longer than `defaultPendingMs` to load; the pages use the same skeleton while
// their own data is missing (a workspace switch).

import type { ReactNode } from 'react'
import { Skeleton } from '@/components/ui/skeleton'
import { usePageHeader } from '../shell/ShellUi'
import { TABS } from './settings/tabs'
import { WIDE_QUERY } from './today/shared'

/** As the router's pending component the skeleton sets the topbar title; inside a page (`inPage`) the page does. */
function Title({ title }: { title: string }) {
  usePageHeader(title)
  return null
}

/** Where a skeleton is shown: by the router while the page loads, or by the page itself while its data is missing. */
interface Placement {
  inPage?: boolean
}

function Loading({ title, children, className, inPage }: { title: string; children: ReactNode; className?: string } & Placement) {
  return (
    <div role="status" aria-label="Loading page" aria-busy="true" className={className}>
      {!inPage && <Title title={title} />}
      {children}
    </div>
  )
}

const H1 = ({ children }: { children: ReactNode }) => <h1 className="text-xl font-semibold tracking-tight">{children}</h1>

/** A bordered panel with a header line and `rows` rows: the shape of Today's groups and side panels. */
function Panel({ rows, rowH = 'h-11' }: { rows: number; rowH?: string }) {
  return (
    <div className="overflow-hidden rounded-lg border border-border bg-surface">
      <div className="border-b border-border px-3 py-2.5">
        <Skeleton className="h-4 w-32" />
      </div>
      <div className="divide-y divide-border">
        {Array.from({ length: rows }, (_, i) => (
          <div key={i} className={`flex items-center gap-3 px-3 ${rowH}`}>
            <Skeleton className="h-3.5 w-16" />
            <Skeleton className="h-3.5 flex-1" />
          </div>
        ))}
      </div>
    </div>
  )
}

export function TodaySkeleton({ wide = typeof window === 'undefined' || window.matchMedia?.(WIDE_QUERY).matches !== false, inPage }: { wide?: boolean } & Placement) {
  const queue = (
    <div className="min-w-0 space-y-3">
      <Panel rows={3} />
      <Panel rows={2} />
    </div>
  )
  return (
    <Loading title="Today" className="space-y-5" inPage={inPage}>
      <div className="space-y-1">
        <H1>Today</H1>
        <Skeleton className="my-0.5 h-4 w-72" />
      </div>
      {wide ? (
        <div className="grid grid-cols-[minmax(0,1fr)_320px] gap-6">
          {queue}
          <div className="min-w-0 space-y-4">
            <Panel rows={3} />
            <Panel rows={3} rowH="h-10" />
          </div>
        </div>
      ) : (
        <div className="space-y-4">
          <Skeleton className="h-10 w-full" />
          {queue}
        </div>
      )}
    </Loading>
  )
}

function Toolbar() {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Skeleton className="h-8 w-56" />
      <Skeleton className="h-8 w-[110px]" />
      <Skeleton className="h-8 w-[110px]" />
      <Skeleton className="h-8 w-[110px]" />
      <span className="flex-1" />
      <Skeleton className="h-8 w-24" />
    </div>
  )
}

/** The board's columns while its tickets load (under the real toolbar, or the skeleton's). */
export function BoardColumnsSkeleton() {
  return (
    <div role="status" aria-label="Loading tickets" className="flex min-w-0 items-start gap-2 overflow-hidden">
      {[4, 3, 2, 3, 1].map((n, i) => (
        <div key={i} className="w-[272px] shrink-0 space-y-2 rounded-lg bg-surface-2/50 p-2">
          <Skeleton className="h-5 w-28" />
          {Array.from({ length: n }, (_, j) => (
            <Skeleton key={j} className="h-[88px] w-full" />
          ))}
        </div>
      ))}
    </div>
  )
}

export function BoardSkeleton({ inPage }: Placement = {}) {
  return (
    <Loading title="Board" className="flex min-h-0 flex-col gap-4" inPage={inPage}>
      <Toolbar />
      <BoardColumnsSkeleton />
    </Loading>
  )
}

export function TicketsSkeleton({ inPage }: Placement = {}) {
  return (
    <Loading title="Tickets" className="space-y-3" inPage={inPage}>
      <Toolbar />
      <TicketRowsSkeleton />
    </Loading>
  )
}

/** The tickets table while its rows load (under the real filters, or the skeleton's). */
export function TicketRowsSkeleton() {
  return (
    <div role="status" aria-label="Loading tickets" className="overflow-hidden rounded-lg border border-border bg-surface">
        <div className="flex h-[38px] items-center border-b border-border px-3">
          <Skeleton className="h-3.5 w-1/3" />
        </div>
        {Array.from({ length: 8 }, (_, i) => (
          <div key={i} className="flex h-[60px] items-center gap-3 border-b border-border px-3 last:border-0">
            <Skeleton className="h-3.5 w-20" />
            <div className="flex-1 space-y-2">
              <Skeleton className="h-3.5 w-2/3" />
              <Skeleton className="h-3 w-1/3" />
            </div>
            <Skeleton className="h-3.5 w-24" />
          </div>
        ))}
    </div>
  )
}

export function TicketSkeleton({ title, inPage }: { title: string } & Placement) {
  return (
    <Loading title={title} className="mx-auto min-w-0 max-w-[1280px] space-y-4" inPage={inPage}>
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
    </Loading>
  )
}

export function AgentsSkeleton({ inPage }: Placement = {}) {
  return (
    <Loading title="Agents" className="max-w-[1040px] space-y-5" inPage={inPage}>
      <div>
        <H1>Agents</H1>
        <Skeleton className="mt-1 h-4 w-80" />
      </div>
      <Panel rows={3} />
      <Panel rows={2} />
    </Loading>
  )
}

export function SettingsSkeleton({ inPage }: Placement = {}) {
  return (
    <Loading title="Settings" className="flex max-w-5xl flex-col gap-4 @[60rem]/page:flex-row @[60rem]/page:gap-8" inPage={inPage}>
      <div className="flex flex-wrap items-center gap-0.5 @[60rem]/page:block @[60rem]/page:w-48 @[60rem]/page:shrink-0 @[60rem]/page:space-y-0.5">
        <div className="mb-1 flex w-full items-center gap-1 @[60rem]/page:mb-2 @[60rem]/page:px-2.5">
          <H1>Settings</H1>
        </div>
        {/* The section names are known up front: the sub-nav is drawn for real, only the content waits. */}
        {TABS.map((t) => (
          <div key={t.id} className="flex items-center gap-2 rounded-md px-2.5 py-1.5 text-[13px] text-text-muted">
            <span className="flex-1">{t.label}</span>
          </div>
        ))}
      </div>
      <div className="min-w-0 flex-1 space-y-4">
        <Skeleton className="h-6 w-48" />
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    </Loading>
  )
}

export function AddonPageSkeleton({ title = '', inPage }: { title?: string } & Placement) {
  return (
    <Loading title={title} className="w-full space-y-4" inPage={inPage}>
      <div className="flex h-[41px] items-center gap-2 border-b border-border pb-3">
        <Skeleton className="size-5" />
        <Skeleton className="h-6 w-48" />
      </div>
      <Skeleton className="h-40 w-full" />
      <Skeleton className="h-24 w-full" />
    </Loading>
  )
}

/** The artifacts results while they load: the table's rows (list) or cards (grid), under the real toolbar. */
export function ArtifactsBodySkeleton({ view }: { view: 'list' | 'grid' }) {
  if (view === 'grid')
    return (
      <div role="status" aria-label="Loading artifacts" className="grid grid-cols-2 gap-3 @[44rem]/page:grid-cols-3 @[64rem]/page:grid-cols-4">
        {Array.from({ length: 8 }, (_, i) => (
          <div key={i} className="space-y-2 rounded-lg border border-border bg-surface p-2.5">
            <Skeleton className="h-24 w-full" />
            <Skeleton className="h-3.5 w-3/4" />
            <Skeleton className="h-3 w-1/2" />
          </div>
        ))}
      </div>
    )
  return (
    <div role="status" aria-label="Loading artifacts" className="overflow-hidden rounded-lg border border-border bg-surface">
      <div className="flex h-10 items-center border-b border-border px-2">
        <Skeleton className="h-3.5 w-1/3" />
      </div>
      {Array.from({ length: 8 }, (_, i) => (
        <div key={i} className="flex h-[49px] items-center gap-3 border-b border-border px-2 last:border-0">
          <Skeleton className="h-3.5 w-[26%]" />
          <Skeleton className="h-3.5 w-16" />
          <Skeleton className="h-3.5 flex-1" />
          <Skeleton className="h-3.5 w-24" />
        </div>
      ))}
    </div>
  )
}

export function ArtifactsSkeleton({ view = 'list', inPage }: { view?: 'list' | 'grid' } & Placement) {
  return (
    <Loading title="Artifacts" className="space-y-4" inPage={inPage}>
      <div className="flex flex-wrap items-center gap-3">
        <H1>Artifacts</H1>
        <span className="flex-1" />
        <Skeleton className="h-8 w-[132px]" />
      </div>
      <p className="-mt-2 text-[13px] text-text-muted">Evidence, logs, screenshots and reports from the tickets you can see in this workspace.</p>
      <div className="flex flex-wrap items-center gap-2">
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-8 w-[110px]" />
        <Skeleton className="h-8 w-[110px]" />
        <Skeleton className="h-8 w-[110px]" />
        <Skeleton className="h-8 w-[110px]" />
      </div>
      <ArtifactsBodySkeleton view={view} />
    </Loading>
  )
}

/** The fallback for a page without its own skeleton (a heading line and one block). */
export function GenericSkeleton() {
  return (
    <div className="space-y-4" role="status" aria-label="Loading page" aria-busy="true">
      <Skeleton className="h-7 w-48" />
      <Skeleton className="h-40 w-full" />
    </div>
  )
}
