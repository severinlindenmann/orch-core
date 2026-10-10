import type { RJSFValidationError } from '@rjsf/utils'
import { useQuery } from '@tanstack/react-query'
import { Link, useBlocker, useRouter } from '@tanstack/react-router'
import { createContext, lazy, Suspense, useCallback, useContext, useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { ArrowRight, ChevronRight, Copy, Ellipsis, ExternalLink, TriangleAlert } from 'lucide-react'
import { addonActive } from '@/api/addons'
import { api } from '@/api/client'
import { useWorkspace } from '@/app/workspace'
import { Badge } from '@/components/ui/badge'
import { STATUS_LABEL } from '@/app/pages/ticket/shared'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { cn } from '@/lib/utils'
import { AddonBadge } from './AddonBadge'
import { AddonChart } from './AddonChart'
import { canUsePty } from './capabilities'
import { FrameNode } from './FrameNode'
import { CodeBlock } from './CodeBlock'
import { INTERNAL_LINK, MAX_DEPTH, parseNode, type ItemAction, type NodeOf } from './nodes'
import { SafeMarkdown } from './SafeMarkdown'
import { useAddons, type SlotContext } from './slots'
import { ErrorAlert } from './ErrorAlert'
import { roleReason, useRunAddonAction, type ActionError } from './useRunAddonAction'
import { useRole } from '@/app/useRole'
import { precheckReason } from './SpawnConfirm'
import { DestructiveConfirm } from './DestructiveConfirm'
import { Collapse } from '@/components/Collapse'
import { foldedColumns } from '@/lib/columnFold'
import { useElementWidth } from '@/lib/useElementWidth'
import { Sparkline } from '@/components/Sparkline'
import { visible } from '@/components/sign/visible'

// rjsf (with ajv) loads on first form, so it stays out of the main bundle.
const ThemedForm = lazy(() => import('./AddonForm'))
// The whole terminal module (xterm included) loads on first use, so the main bundle does not grow.
const DecisionNode = lazy(() => import('./DecisionNode'))
const TerminalView = lazy(() => import('@/app/terminal/TerminalView'))
// Ticket widgets (parser, core types, template frames) load on first use too.
const WidgetNodeView = lazy(() => import('@/app/pages/ticket/widgets/WidgetNode'))
const WidgetIndexView = lazy(() => import('@/app/pages/ticket/widgets/WidgetNode').then((m) => ({ default: m.WidgetIndex })))

interface Runtime {
  addon: string
  ctx: SlotContext
  compact: boolean
  /** Drawn as one calm line of a glance list (Today): a stat without its box, a list cut to GLANCE_ROWS. */
  glance?: Glance
  /** Core says the viewer may not change anything: forms, buttons and item actions render disabled. */
  readOnly: boolean
  formControl?: FormControl
  /** Submit actions of forms on this page that hold unsaved edits (a form with `cancel`). Any other action asks before it discards them. */
  dirty: Set<string>
  /** Decisions (by id) whose terms a form with unsaved edits sets (the form's `guards`). */
  guardedDecisions: Set<string>
  /** Told whenever `guardedDecisions` changes. */
  dirtyListeners: Set<() => void>
  /** Which form took the drawer's form control (see FormNode), and the forms waiting to take it when it is released. */
  formControlClaim: FormControlClaim
}
/** Where the glance's "n more" leads (the addon's own page), when it has one. */
export interface Glance {
  open?: { name: string; page: string }
}
/** Rows a list shows in a glance before "n more". */
export const GLANCE_ROWS = 3
interface FormControlClaim {
  current: string | null
  waiting: Set<() => void>
}
const newClaim = (): FormControlClaim => ({ current: null, waiting: new Set() })
const RuntimeCtx = createContext<Runtime>({ addon: '', ctx: {}, compact: false, readOnly: false, dirty: new Set(), guardedDecisions: new Set(), dirtyListeners: new Set(), formControlClaim: newClaim() })

/** Does a form on this page that sets this decision's terms hold unsaved edits right now? Re-renders when that changes. */
function useUnsavedTerms(id: string): boolean {
  const { guardedDecisions, dirtyListeners } = useContext(RuntimeCtx)
  const [unsaved, setUnsaved] = useState(guardedDecisions.has(id))
  useEffect(() => {
    const check = () => setUnsaved(guardedDecisions.has(id))
    dirtyListeners.add(check)
    check()
    return () => void dirtyListeners.delete(check)
  }, [id, guardedDecisions, dirtyListeners])
  return unsaved
}

/** A decision node: core's row; its primary option (the one that grants) is off while a form that sets its terms (`guards`) holds unsaved edits, which would otherwise not be in what is signed. Other options stay. */
function GuardedDecision({ addon, id }: { addon: string; id: string }) {
  const unsaved = useUnsavedTerms(id)
  return <DecisionNode addon={addon} id={id} blockedReason={unsaved ? 'Unsaved changes to the terms: save or cancel them before you accept.' : undefined} />
}

/** A stack with nothing in it draws nothing and takes no gap (addons use one as "no alert right now"). */
const isEmptyStack = (c: unknown) => !!c && typeof c === 'object' && (c as { type?: unknown }).type === 'stack' && Array.isArray((c as { children?: unknown }).children) && (c as { children: unknown[] }).children.length === 0

/**
 * A form node whose submit button the caller draws itself (a drawer footer): the form gets this `id`, renders no
 * submit button, and reports whether a save is running or not allowed so the caller's `<button type="submit" form={id}>` can follow.
 */
export interface FormControl {
  id: string
  onState: (s: { pending: boolean; blocked: boolean }) => void
  /** The form's action went through (a refusal does not call it): the drawer closes itself. */
  onSaved?: () => void
}

/** Box shown instead of anything that is not one of the allowed node types or fails validation. */
export function AddonUnavailable({ addon }: { addon: string }) {
  return (
    <div role="alert" className="flex items-center gap-2 rounded-md border border-dashed border-addon-border px-3 py-2 text-[12px] text-text-muted">
      <AddonBadge name={addon} />
      <TriangleAlert className="size-3.5 text-warning" />
      This addon panel could not be shown.
    </div>
  )
}

/**
 * Renders one declarative node tree from an addon. The node is untrusted: it is validated against the closed
 * set of node types (nodes.ts) and anything unknown or malformed becomes the "could not be shown" box.
 */
export function AddonNode({ node, addon, ctx = {}, compact = false, readOnly = false, formControl, glance }: { node: unknown; addon: string; ctx?: SlotContext; compact?: boolean; readOnly?: boolean; formControl?: FormControl; glance?: Glance }) {
  const dirty = useRef(new Set<string>()).current
  const guardedDecisions = useRef(new Set<string>()).current
  const dirtyListeners = useRef(new Set<() => void>()).current
  const formControlClaim = useRef(newClaim()).current
  return (
    <RuntimeCtx.Provider value={{ addon, ctx, compact, readOnly, formControl, dirty, guardedDecisions, dirtyListeners, formControlClaim, glance }}>
      <NodeView node={node} depth={0} />
    </RuntimeCtx.Provider>
  )
}

function NodeView({ node: raw, depth }: { node: unknown; depth: number }) {
  const { addon, glance } = useContext(RuntimeCtx)
  const parsed = depth > MAX_DEPTH ? ({ ok: false } as const) : parseNode(raw)
  if (!parsed.ok) return <AddonUnavailable addon={addon} />
  const n = parsed.node
  switch (n.type) {
    case 'stack':
      // A row shares the width; in a narrow page area its children wrap once each would get under 12rem (N11).
      return (
        <div className={cn('flex gap-3', n.direction === 'row' ? (n.fit ? 'flex-row flex-wrap items-center [&>*]:min-w-0' : 'flex-row flex-wrap [&>*]:min-w-0 [&>*]:flex-[1_1_12rem]') : 'flex-col')}>
          {n.children.filter((c) => !isEmptyStack(c)).map((c, i) => (
            <NodeView key={i} node={c} depth={depth + 1} />
          ))}
        </div>
      )
    case 'stat':
      return <Stat node={n} />
    case 'kv':
      return (
        <dl className="grid grid-cols-[auto_1fr] content-start gap-x-4 gap-y-1.5 text-[13px]">
          {n.pairs.map((p, i) => (
            <div key={i} className="contents">
              <dt className="text-text-muted">{p.label}</dt>
              <dd className={cn('text-text', p.mono && 'font-mono text-[12px]')}>{p.value === null || p.value === '' ? '–' : String(p.value)}</dd>
            </div>
          ))}
        </dl>
      )
    case 'list':
      if (glance) return <GlanceList n={n} open={glance.open} />
      return n.items.length === 0 ? (
        <p className="text-[13px] text-text-faint">{n.empty ?? 'Nothing here.'}</p>
      ) : (
        <ul className="divide-y divide-border">
          {stableKeys(n.items.map((it) => it.id ?? it.actions?.[0]?.args?.id ?? it.title)).map((k, i) => (
            n.items[i].actions ? <ActionListItem key={k} it={n.items[i]} /> : <ListItemView key={k} it={n.items[i]} />
          ))}
        </ul>
      )
    case 'table':
      if (n.rows.length === 0) return <p className="text-[13px] text-text-faint">{n.empty ?? 'Nothing here.'}</p>
      return <TableNodeView n={n} depth={depth} />
    case 'markdown':
      return (
        <div className={n.toc ? undefined : 'max-w-[72ch]'}>
          <SafeMarkdown text={n.text} toc={n.toc} />
        </div>
      )
    case 'code':
      return <CodeBlock language={n.language} text={n.text} />
    case 'chart':
      return <AddonChart node={n} />
    case 'form':
      return <FormNode node={n} />
    case 'button':
      return <ButtonNode node={n} />
    case 'alert':
      return <AlertNode node={n} />
    case 'progress':
      return <ProgressNode node={n} />
    case 'frame':
      return (
        <SandboxedFrame addon={addon}>
          <FrameNode node={n} fallback={<AddonUnavailable addon={addon} />} />
        </SandboxedFrame>
      )
    case 'terminal':
      return <TerminalNode session={n.session} />
    case 'decision':
      return (
        <Suspense fallback={<Skeleton className="h-12 w-full" />}>
          <GuardedDecision addon={addon} id={n.id} />
        </Suspense>
      )
    case 'widget':
      return (
        <Suspense fallback={<Skeleton className="h-24 w-full" />}>
          <WidgetNodeView block={n.block} source={n.source} addon={addon} />
        </Suspense>
      )
    case 'widget-index':
      return (
        <Suspense fallback={null}>
          <WidgetIndexView groups={n.groups} />
        </Suspense>
      )
    case 'tabs':
      return <TabsView node={n} depth={depth} />
    case 'fold':
      return <FoldView node={n} depth={depth} />
    case 'popover':
      return <PopoverView node={n} depth={depth} />
    case 'link':
      if (INTERNAL_LINK.test(n.href)) return <InternalLink href={n.href} label={n.label} />
      if (n.copy) return <CopyableLink href={n.href} label={n.label} />
      return (
        <a href={n.href} target="_blank" rel="noopener noreferrer nofollow" className="inline-flex items-center gap-1 text-[13px] text-brand hover:underline">
          {n.label}
          <ExternalLink className="size-3" />
        </a>
      )
  }
}

/** Row keys that survive an insert or removal (a row's own id or first cell), so a row's error and dialogs stay with it; repeats get a suffix. */
function stableKeys(ids: unknown[]): string[] {
  const seen = new Map<string, number>()
  return ids.map((id) => {
    const base = id === null || id === undefined || id === '' ? 'row' : String(id)
    const n = seen.get(base) ?? 0
    seen.set(base, n + 1)
    return n ? `${base}#${n}` : base
  })
}

/** The tab a person last had open, per workspace, addon and node id. Core-only UI state; storage may be unavailable. */
const tabKey = (ws: string, person: string, addon: string, id: string) => `orch.addon-tab.${ws}.${person}.${addon}.${id}`
function readTab(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

/** Only the open tab's node is rendered, so a hidden tab costs nothing. The remembered tab is the viewer's own. */
function TabsView({ node, depth }: { node: NodeOf<'tabs'>; depth: number }) {
  const { addon } = useContext(RuntimeCtx)
  const { workspace } = useWorkspace()
  const me = useQuery({ queryKey: ['me'], queryFn: api.getMe })
  // The remembered tab is read once we know who is looking (the viewer is cached after the first load); if that fails, nobody in particular.
  if (!me.data && !me.isError) return <Skeleton className="h-8 w-full" />
  const key = tabKey(workspace?.id ?? '', me.data?.person ?? '', addon, node.id)
  // Keyed: another person (or workspace) starts from their own remembered tab.
  return <TabsBody key={key} storageKey={key} node={node} depth={depth} />
}

/**
 * On an addon page the open tab is also in the address (`?tab.<node id>=<tab id>`), so a copied link opens it; an
 * unknown tab id there falls back like any other. Elsewhere (Today, a ticket) only the remembered tab applies.
 */
function useTabInUrl(nodeId: string) {
  const router = useRouter({ warn: false })
  const loc = router?.state.location
  const onPage = !!loc && loc.pathname.startsWith('/addon/')
  const param = `tab.${nodeId}`
  const fromUrl = onPage ? (loc.search as Record<string, unknown>)[param] : undefined
  const write = (id: string) => {
    if (onPage) void router.navigate({ to: '.', search: (prev: Record<string, unknown>) => ({ ...prev, [param]: id }), replace: true } as never)
  }
  return [typeof fromUrl === 'string' ? fromUrl : null, write] as const
}

function TabsBody({ storageKey, node, depth }: { storageKey: string; node: NodeOf<'tabs'>; depth: number }) {
  const [urlTab, writeUrlTab] = useTabInUrl(node.id)
  const [chosen, setChosen] = useState<string | null>(() => urlTab ?? readTab(storageKey))
  const active = node.tabs.find((t) => t.id === chosen) ?? node.tabs[0]
  const pick = (id: string) => {
    setChosen(id)
    writeUrlTab(id)
    try {
      localStorage.setItem(storageKey, id)
    } catch {
      /* the choice lasts for this page only */
    }
  }
  return (
    <Tabs value={active.id} onValueChange={pick} className="gap-3">
      <TabsList variant="line" className="h-8 w-full justify-start border-b border-border p-0">
        {node.tabs.map((t) => (
          <TabsTrigger key={t.id} value={t.id} className="h-8 flex-none rounded-none px-3 text-[13px] after:bottom-[-1px]">
            {t.label}
            {t.count !== null && t.count !== undefined && <span className="font-mono text-[11px] tabular-nums text-text-faint">{t.count}</span>}
          </TabsTrigger>
        ))}
      </TabsList>
      <TabsContent value={active.id} className="min-w-0">
        <NodeView node={active.node} depth={depth + 1} />
      </TabsContent>
    </Tabs>
  )
}

/** Lets a form inside a popover close it once its action has gone through. */
const ClosePopoverCtx = createContext<(() => void) | null>(null)

function PopoverView({ node, depth }: { node: NodeOf<'popover'>; depth: number }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="flex h-full items-end">
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button size="sm" variant={BUTTON_VARIANT[node.variant]}>
          {node.label}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-[26rem] max-w-[90vw]">
        <ClosePopoverCtx.Provider value={() => setOpen(false)}>
          <NodeView node={node.node} depth={depth + 1} />
        </ClosePopoverCtx.Provider>
      </PopoverContent>
    </Popover>
    </div>
  )
}

/** Closed until opened; the content of a closed fold is not rendered. */
function FoldView({ node, depth }: { node: NodeOf<'fold'>; depth: number }) {
  const [open, setOpen] = useState(false)
  const id = useId()
  return (
    <div>
      <button type="button" aria-expanded={open} aria-controls={id} onClick={() => setOpen((o) => !o)} className="inline-flex items-center gap-1.5 text-[13px] text-text-muted hover:text-text focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring">
        <ChevronRight aria-hidden className={cn('size-3.5 transition-transform', open && 'rotate-90')} />
        {node.label}
        {node.count !== null && node.count !== undefined && <span className="font-mono text-[11px] tabular-nums text-text-faint">{node.count}</span>}
      </button>
      <Collapse open={open} id={id}>
        <div className="mt-2">
          <NodeView node={node.node} depth={depth + 1} />
        </div>
      </Collapse>
    </div>
  )
}

/** A list as glance rows: one line each (title, the badge as quiet text), at most GLANCE_ROWS, then "n more". */
function GlanceList({ n, open }: { n: NodeOf<'list'>; open?: Glance['open'] }) {
  if (n.items.length === 0) return <p className="text-[12px] text-text-faint">{n.empty ?? 'Nothing here.'}</p>
  const rest = n.items.length - GLANCE_ROWS
  const more = `${rest} more`
  const shown = n.items.slice(0, GLANCE_ROWS)
  const keys = stableKeys(shown.map((it) => it.id ?? it.title))
  return (
    <div>
      <ul className="space-y-1">
        {shown.map((it, i) => (
          <li key={keys[i]} className="flex min-w-0 items-baseline gap-2 text-[13px] leading-5">
            {it.status && <StatusDot status={it.status} className="self-center" />}
            <span className="min-w-0 flex-1 truncate text-text" title={it.subtitle ? `${it.title} · ${it.subtitle}` : it.title}>
              {it.title}
            </span>
            {it.badge && <span className="shrink-0 text-[12px] text-text-muted">{it.badge}</span>}
          </li>
        ))}
      </ul>
      {rest > 0 &&
        (open ? (
          <Link to="/addon/$name/$page" params={open} className="mt-1 inline-block rounded text-[12px] text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-ring">
            {more}
          </Link>
        ) : (
          <p className="mt-1 text-[12px] text-text-muted">{more}</p>
        ))}
    </div>
  )
}

const trendNumber = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 })
const trendLabel = (t: number[]) => `Trend over ${t.length} values, from ${trendNumber.format(t[0])} to ${trendNumber.format(t[t.length - 1])}`

function Stat({ node }: { node: NodeOf<'stat'> }) {
  const { compact, glance } = useContext(RuntimeCtx)
  const value = node.value === null || node.value === '' ? '–' : node.value
  if (glance)
    return (
      <div className="flex min-w-0 items-center gap-3">
        <div className="min-w-0 flex-1">
          <p className="truncate text-[13px] leading-5">
            <span className="text-[15px] font-semibold tabular-nums text-text">{value}</span>
            {node.label && <span className="text-text-muted"> {node.label}</span>}
          </p>
          {node.hint && <p className="truncate text-[12px] leading-4 text-text-muted">{node.hint}</p>}
        </div>
        {node.trend && node.trend.length > 1 && <Sparkline values={node.trend} label={trendLabel(node.trend)} />}
      </div>
    )
  if (compact)
    return (
      <span className="inline-flex items-baseline gap-1 text-[12px]">
        <span className="font-semibold text-text">{value}</span>
        <span className="text-text-faint">{node.label}</span>
      </span>
    )
  return (
    <div className="rounded-md border border-border bg-bg px-3 py-2.5">
      <div className="text-[12px] text-text-muted">{node.label}</div>
      <div className="mt-0.5 flex items-center justify-between gap-3">
        <span className="text-xl font-semibold tracking-tight text-text">{value}</span>
        {node.trend && node.trend.length > 1 && <Sparkline values={node.trend} label={trendLabel(node.trend)} />}
      </div>
      {node.hint && <div className="mt-0.5 text-[12px] text-text-faint">{node.hint}</div>}
    </div>
  )
}

/**
 * Runs an action of this addon in the current workspace, through the one action hook (`useRunAddonAction`).
 * `blocked`: no workspace yet, the viewer's role is below the manifest's minRole, or core says read-only (a read-only
 * surface still runs the actions the package declares with minRole 'viewer', e.g. navigation).
 */
interface AddonAction {
  run: (action: string, extra?: Record<string, unknown>, subject?: string) => void
  pending: boolean
  pendingAction: string | null
  blocked: boolean
  blockedFor: (action: string) => boolean
  dialog: ReactNode
  precheck: string | null
  error: ActionError | null
  dismissError: () => void
  /** Set only on the row's inline field: closes it without running anything. */
  onCancelAsk?: () => void
  /** Why this action is off for the viewer, in plain words (null when it can run or only waits for the host). */
  reasonFor: (action: string) => string | null
  /** A form may apply on change only for a navigation action with no core dialog (no sign, start or decision step). */
  liveAllowed: (action: string) => boolean
}
function useAddonAction(action?: string, onDone?: () => void): AddonAction {
  const { addon, ctx, readOnly, dirty } = useContext(RuntimeCtx)
  const { workspace } = useWorkspace()
  // An action that would leave unsaved edits behind asks first (the form's own submit does not).
  const [discard, setDiscard] = useState<(() => void) | null>(null)
  // A refusal shows as a persistent alert under the node or row that asked (no toast).
  const r = useRunAddonAction(ctx.ticket?.key, { inlineErrors: true, onSuccess: onDone })
  const role = useRole()
  const blockedFor = (a?: string) => !a || !r.allowed(addon, a) || (readOnly && r.meta(addon, a)?.minRole !== 'viewer')
  const reasonFor = (a: string) => (!r.allowed(addon, a) ? roleReason(role, r.meta(addon, a)?.minRole ?? 'member') : blockedFor(a) ? 'This view is read-only.' : null)
  // Core's prechecks before its start dialog (from the ticket, not the addon): a claimed ticket gets no second agent,
  // and a ticket whose needed connection fails auth or identity gets none until an owner logs in again.
  const precheck =
    action && r.meta(addon, action)?.confirm === 'spawn_agent' && ctx.ticket
      ? precheckReason(ctx.ticket, (id) => workspace?.members.find((m) => m.person === id)?.name ?? id)
      : null
  const run = (a: string, extra?: Record<string, unknown>, subject?: string) => {
    if (dirty.size > 0 && !dirty.has(a)) setDiscard(() => () => r.run(addon, a, extra, subject))
    else r.run(addon, a, extra, subject)
  }
  const dialog = (
    <>
      {r.dialog}
      {discard && (
        <DestructiveConfirm
          label="Discard changes"
          text="You have unsaved edits on this page. They are lost if you go on."
          onConfirm={() => {
            const go = discard
            setDiscard(null)
            go()
          }}
          onClose={() => setDiscard(null)}
        />
      )}
    </>
  )
  const liveAllowed = (a: string) => {
    const m = r.meta(addon, a)
    return m?.kind === 'navigation' && !m.confirm && !m.decision
  }
  return { run, pending: r.pending, pendingAction: r.pendingAction, blocked: action ? blockedFor(action) : readOnly, blockedFor, reasonFor, dialog, precheck, error: r.error, dismissError: r.dismissError, liveAllowed }
}

const BUTTON_VARIANT = { primary: 'default', secondary: 'secondary', ghost: 'ghost', danger: 'destructive' } as const

/** Core's reason an agent cannot start here (claimed ticket, blocked connection), shown at the control. */
function PrecheckAlert({ id, text }: { id?: string; text: string }) {
  return (
    <p id={id} role="alert" className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning-soft px-2.5 py-1.5 text-[12px] text-text">
      <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-warning" aria-hidden />
      {text}
    </p>
  )
}

function ButtonNode({ node }: { node: NodeOf<'button'> }) {
  const { run, pending, blocked, dialog, precheck, error, dismissError, reasonFor } = useAddonAction(node.action)
  const reasonId = useId()
  // A button that is off for the viewer's role says why (a tooltip on its wrapper: the disabled button gets no pointer events).
  const why = blocked && !pending ? reasonFor(node.action) : null
  return (
    // `flex-none!`: in a row stack a button keeps its own width instead of stretching like a panel does.
    <div className="space-y-2 flex-none!">
      {precheck && <PrecheckAlert id={reasonId} text={precheck} />}
      <span title={why ?? (!precheck ? node.disabled : undefined)} className="inline-flex">
        <Button
          size="sm"
          variant={BUTTON_VARIANT[node.variant]}
          disabled={pending || blocked || !!precheck || !!node.disabled}
          aria-describedby={precheck ? reasonId : why ? `${reasonId}-why` : node.disabled ? `${reasonId}-off` : undefined}
          title={!precheck && !why ? node.disabled : undefined}
          aria-pressed={node.pressed}
          className={node.pressed ? 'border border-border-strong bg-surface-3' : undefined}
          onClick={() => run(node.action, node.args)}
        >
          {node.label}
        </Button>
        {why && (
          <span id={`${reasonId}-why`} className="sr-only">
            {why}
          </span>
        )}
      </span>
      {node.disabled && !precheck && !why && <p id={`${reasonId}-off`} className="text-[12px] text-text-muted">{node.disabled}</p>}
      {error && <ErrorAlert error={error} onDismiss={dismissError} />}
      {dialog}
    </div>
  )
}

/**
 * Focus the first field with an error. rjsf's own focus does `field.length ? field[0]`, and a native select has a
 * `length` (its options), so it focuses an option and nothing happens: look the element up by id here.
 */
function focusField(error: RJSFValidationError) {
  const id = `root_${(error.property ?? '').replace(/^\./, '').replace(/\./g, '_')}`
  document.getElementById(id)?.focus()
}

/** "Fill in Ticket" for a missing required field, in the field's own title (rjsf says "must have required property"). */
function formErrors(errors: RJSFValidationError[], schema: Record<string, unknown>): RJSFValidationError[] {
  return errors.map((raw) => {
    // rjsf names the field ".ticket"; its focusOnFirstError builds the element id from that and would look for "root__ticket" and focus nothing; drop the dot.
    const e = raw.property?.startsWith('.') ? { ...raw, property: raw.property.slice(1) } : raw
    if (e.name !== 'required') return e
    const field = e.params?.missingProperty as string | undefined
    const title = (schema as { properties?: Record<string, { title?: string }> }).properties?.[field ?? '']?.title ?? field ?? 'this field'
    return { ...e, message: `Fill in ${title}` }
  })
}

/** While a form holds unsaved edits, leaving the page by the router (links, palette, shortcuts, Back) asks first. Mounted only then. */
function LeaveGuard() {
  const blocker = useBlocker({ shouldBlockFn: () => true, withResolver: true, enableBeforeUnload: false })
  if (blocker.status !== 'blocked') return null
  return (
    <Dialog open onOpenChange={(o) => !o && blocker.reset()}>
      <DialogContent className="max-w-md border-border bg-surface">
        <DialogHeader>
          <DialogTitle>Discard unsaved changes?</DialogTitle>
          <DialogDescription>This page has changes that are not saved.</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="ghost" onClick={() => blocker.reset()}>
            Keep editing
          </Button>
          <Button variant="destructive" onClick={() => blocker.proceed()}>
            Discard changes
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

const initial0 = (node: NodeOf<'form'>) => JSON.stringify(node.formData ?? {})
/** JSON with object keys sorted: the form's data counts as edited only when a value changed, not when the form reorders keys. */
const stableJson = (v: unknown): string =>
  JSON.stringify(v, (_k, x) => (x && typeof x === 'object' && !Array.isArray(x) ? Object.fromEntries(Object.entries(x as Record<string, unknown>).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))) : x))
/** A free-text field of a form schema (no choices): typing in it waits for a pause before a live form runs. */
const isTextField = (schema: Record<string, unknown>, key: string) => {
  const p = ((schema.properties ?? {}) as Record<string, Record<string, unknown>>)[key]
  return !!p && p.type === 'string' && !p.enum && !p.oneOf
}

function FormNode({ node }: { node: NodeOf<'form'> }) {
  const closePopover = useContext(ClosePopoverCtx)
  // In a popover the form closes it once its action went through (a refusal leaves it open, with the error in it).
  // `reset`: once the action went through the fields are emptied (the form remounts) and the first is focused again.
  const [round, setRound] = useState(0)
  const box = useRef<HTMLDivElement>(null)
  const saved = useRef<(() => void) | undefined>(undefined)
  const { run, pending, blocked: roleBlocked, blockedFor, dialog, precheck, error, dismissError, liveAllowed } = useAddonAction(node.action, () => {
    closePopover?.()
    if (node.reset) setRound((n) => n + 1)
    saved.current?.()
  })
  useEffect(() => {
    if (round > 0) box.current?.querySelector<HTMLElement>('input:not([type=hidden]),select,textarea')?.focus()
  }, [round])
  // `live` is honoured only for a navigation action without a core dialog; otherwise the form keeps its submit button.
  const live = !!node.live && liveAllowed(node.action)
  const { dirty, guardedDecisions, dirtyListeners } = useContext(RuntimeCtx)
  // Core's spawn_agent precheck applies to a form that starts an agent as it does to a button.
  const blocked = roleBlocked || !!precheck
  const readOnly = blocked
  const { formControl: offered, formControlClaim } = useContext(RuntimeCtx)
  // The drawer's form control belongs to ONE form: the first that mounts. Another form on the page keeps its own submit button.
  const me = useId()
  // Not 'won' until the claim is made (before paint), so a form that loses never reports its state in between.
  // A form that lost waits, and takes the control when the winner goes away.
  const [claim, setClaim] = useState<'won' | 'lost' | null>(null)
  useLayoutEffect(() => {
    if (!offered) {
      setClaim(null)
      return
    }
    const take = () => {
      if (formControlClaim.current === null || formControlClaim.current === me) {
        formControlClaim.current = me
        formControlClaim.waiting.delete(take)
        setClaim('won')
      } else {
        formControlClaim.waiting.add(take)
        setClaim('lost')
      }
    }
    take()
    return () => {
      formControlClaim.waiting.delete(take)
      if (formControlClaim.current === me) {
        formControlClaim.current = null
        for (const next of [...formControlClaim.waiting]) next() // the first waiting form takes it; the rest stay waiting
      }
    }
  }, [offered, formControlClaim, me])
  const formControl = claim === 'won' ? offered : undefined
  saved.current = formControl?.onSaved
  const report = formControl?.onState
  useEffect(() => report?.({ pending, blocked }), [report, pending, blocked])
  const submitOptions = formControl || live ? { norender: true } : { submitText: node.submitLabel ?? 'Save', props: { disabled: pending || blocked } }
  // A live form (filters) runs its action on each change: a choice at once, typed text after a pause.
  const liveTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const liveLast = useRef(initial0(node))
  useEffect(() => () => void (liveTimer.current && clearTimeout(liveTimer.current)), [])
  const onLive = (formData: Record<string, unknown> | undefined) => {
    const next = formData ?? {}
    const prev = JSON.parse(liveLast.current) as Record<string, unknown>
    if (JSON.stringify(next) === liveLast.current) return
    const typed = Object.keys(next).some((k) => next[k] !== prev[k] && isTextField(node.schema, k))
    liveLast.current = JSON.stringify(next)
    if (liveTimer.current) clearTimeout(liveTimer.current)
    liveTimer.current = setTimeout(() => run(node.action, { formData: next }), typed ? 400 : 0)
  }
  const guarded = !!node.cancel
  const initial = stableJson(node.formData ?? {})
  const [edited, setEdited] = useState(false)
  // A guarded form keeps what the person typed or ticked as its data: re-rendering for "Unsaved changes" re-reads the
  // form's props, which would otherwise put the addon's data back over the edit.
  const [draft, setDraft] = useState<Record<string, unknown> | null>(null)
  useEffect(() => {
    setEdited(false) // new data from the addon (e.g. after a save): nothing unsaved any more
    setDraft(null)
  }, [initial])
  useEffect(() => void (liveLast.current = initial), [initial]) // the addon's answer is the new starting point for a live form
  // A form with Cancel tracks unsaved edits: this page's other actions ask before they discard them, and closing the tab does too.
  useEffect(() => {
    if (!guarded) return
    const tell = () => dirtyListeners.forEach((f) => f())
    const guards = node.guards
    if (edited) dirty.add(node.action)
    else dirty.delete(node.action)
    if (guards) {
      if (edited) guardedDecisions.add(guards)
      else guardedDecisions.delete(guards)
    }
    tell()
    const warn = (e: BeforeUnloadEvent) => e.preventDefault()
    if (edited) window.addEventListener('beforeunload', warn)
    return () => {
      dirty.delete(node.action)
      if (guards) guardedDecisions.delete(guards)
      tell()
      window.removeEventListener('beforeunload', warn)
    }
  }, [guarded, edited, dirty, guardedDecisions, dirtyListeners, node.action, node.guards])
  return (
    <>
      {dialog}
      {/* A form that holds the drawer's form control is guarded by the drawer: a second prompt would ask twice. */}
      {guarded && edited && <LeaveGuard />}
      {precheck && <PrecheckAlert text={precheck} />}
      <Suspense fallback={<Skeleton className="h-24 w-full" />}>
        <div ref={box}>
        <ThemedForm
          id={formControl?.id}
          disabled={readOnly}
          // A live form keeps its fields mounted (typing goes on while results update); others start over with new data.
          key={live ? `${round}|live` : `${round}|${JSON.stringify(node.formData ?? null)}`}
          schema={node.schema}
          uiSchema={{ ...node.uiSchema, 'ui:submitButtonOptions': submitOptions }}
          formData={(guarded && draft) || node.formData || undefined}
          noHtml5Validate
          showErrorList={false}
          focusOnFirstError={focusField}
          transformErrors={(errors) => formErrors(errors, node.schema)}
          onChange={guarded ? ({ formData }) => { setDraft(formData as Record<string, unknown>); setEdited(stableJson(formData ?? {}) !== initial) } : live ? ({ formData }) => onLive(formData as Record<string, unknown> | undefined) : undefined}
          onSubmit={({ formData }) => run(node.action, { formData })}
        >
          {node.cancel && !formControl ? (
            <div className="mt-3 flex items-center gap-2">
              <Button type="submit" size="sm" disabled={pending || blocked}>
                {node.submitLabel ?? 'Save'}
              </Button>
              <Button type="button" size="sm" variant="ghost" disabled={pending || blockedFor(node.cancel.action)} onClick={() => run(node.cancel!.action)}>
                {node.cancel.label}
              </Button>
              {edited && (
                <span role="status" className="inline-flex items-center gap-1.5 text-[12px] text-text-muted">
                  <span aria-hidden className="size-1.5 rounded-full bg-text-muted" />
                  Unsaved changes
                </span>
              )}
            </div>
          ) : undefined}
        </ThemedForm>
        </div>
      </Suspense>
      {error && <ErrorAlert error={error} onDismiss={dismissError} className="mt-2" />}
    </>
  )
}

const STATUS_DOT = { ok: 'bg-success', warn: 'bg-warning', error: 'bg-danger', idle: 'bg-text-faint', running: 'bg-info animate-pulse' } as const

function StatusDot({ status, className }: { status: keyof typeof STATUS_DOT; className?: string }) {
  return <span role="img" aria-label={status} className={cn('size-2 shrink-0 rounded-full', STATUS_DOT[status], className)} />
}

/** A `frame` node sits inside a contribution that is already framed: a hairline and a "Sandboxed" chip, no second frame. */
function SandboxedFrame({ addon, children }: { addon: string; children: ReactNode }) {
  return (
    <div data-addon={addon} className="rounded-md border border-addon-border p-1.5">
      <span className="mb-1 inline-flex items-center rounded-full border border-border px-1.5 py-px text-[10px] font-medium leading-4 text-text-muted">Sandboxed</span>
      {children}
    </div>
  )
}

/** Cells in a column keyed `status`/`state` use core's status words ("in-progress" is "In progress"); others are shown as written. */
function cellText(key: string, v: unknown): string {
  if (v === null || v === undefined) return '–'
  const s = String(v)
  return key === 'status' || key === 'state' ? ((STATUS_LABEL as Record<string, string>)[s] ?? s) : s
}

/** What a row is called in "More actions for …": its first column's cell. */
function rowLabel(key: string | undefined, row: Record<string, unknown>): string {
  // A table whose first column repeats (two PRs of one repository) names its rows with a `rowName` cell.
  if (typeof row.rowName === 'string' && row.rowName) return row.rowName
  const v = key ? row[key] : undefined
  return v === null || v === undefined || v === '' ? 'row' : String(v)
}

/** "$row.<key>" args take that row's cell value; a null or missing cell leaves the arg out. */
function resolveRowArgs(args: ItemAction['args'], row?: Record<string, unknown>): Record<string, string | number | boolean> {
  const out: Record<string, string | number | boolean> = {}
  for (const [k, v] of Object.entries(args ?? {})) {
    const ref = typeof v === 'string' ? /^\$row\.(.+)$/.exec(v) : null
    const val = ref ? row?.[ref[1]] : v
    if (typeof val === 'string' || typeof val === 'number' || typeof val === 'boolean') out[k] = val
  }
  return out
}

/** Why an action is blocked on this row (its `blocked` sentence, or the named cell), or null when it can run. */
function blockedWhy(a: ItemAction, row?: Record<string, unknown>): string | null {
  if (!a.blocked) return null
  const ref = /^\$row\.(.+)$/.exec(a.blocked)
  const v = ref ? row?.[ref[1]] : a.blocked
  return typeof v === 'string' && v.trim() ? v : null
}

/** "$row.<key>" / "!$row.<key>": is the action offered for this row? A cell is false when null, empty, false, 0 or "false". */
function offered(when: string | undefined, row?: Record<string, unknown>): boolean {
  if (!when || !row) return true
  const neg = when.startsWith('!')
  const v = row[when.replace(/^!?\$row\./, '')]
  const truthy = !(v === null || v === undefined || v === '' || v === false || v === 0 || v === 'false')
  return neg ? !truthy : truthy
}

type ListEntry = NodeOf<'list'>['items'][number]

function ActionListItem({ it }: { it: ListEntry }) {
  // An action with `input` asks in the row; the row closes it once the action went through.
  const [asking, setAsking] = useState<ItemAction | null>(null)
  return <ListItemView it={it} act={useAddonAction(undefined, () => setAsking(null))} asking={asking} onAsk={setAsking} />
}

/** The one-line field under a list item whose action has `input`: focused, Enter sends, Escape cancels. */
function InlineAsk({ action, act, label }: { action: ItemAction; act: AddonAction; label: string }) {
  const input = action.input!
  const id = useId()
  const [value, setValue] = useState('')
  const ref = useRef<HTMLInputElement>(null)
  useEffect(() => ref.current?.focus(), [])
  return (
    <form
      className="col-span-2 flex flex-wrap items-center gap-2 pb-1"
      onKeyDown={(e) => {
        if (e.key === 'Escape') {
          e.stopPropagation()
          act.onCancelAsk?.()
        }
      }}
      onSubmit={(e) => {
        e.preventDefault()
        if (value.trim()) act.run(action.action, { ...resolveRowArgs(action.args), [input.name]: value.trim() }, label)
      }}
    >
      <label htmlFor={id} className="text-[12px] text-text-muted">
        {input.label}
      </label>
      <Input id={id} ref={ref} name={input.name} value={value} maxLength={input.maxLength ?? 200} placeholder={input.placeholder} onChange={(e) => setValue(e.target.value)} className="h-8 min-w-48 flex-1 text-[13px]" />
      <Button type="submit" size="sm" disabled={act.pending || !value.trim()}>
        {input.submitLabel ?? action.label}
      </Button>
      <Button type="button" size="sm" variant="ghost" onClick={() => act.onCancelAsk?.()}>
        Cancel
      </Button>
    </form>
  )
}

function ListItemView({ it, act, asking, onAsk }: { it: ListEntry; act?: AddonAction; asking?: ItemAction | null; onAsk?: (a: ItemAction | null) => void }) {
  const li = useRef<HTMLLIElement>(null)
  // Cancelling the row's field puts focus back on the button that opened it.
  const cancelAsk = () => {
    const label = asking?.label
    onAsk?.(null)
    setTimeout(() => [...(li.current?.querySelectorAll('button') ?? [])].find((b) => b.textContent?.trim() === label)?.focus(), 0)
  }
  return (
    <li ref={li} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 py-1.5 first:pt-0 last:pb-0">
      <div className="flex min-w-0 items-start gap-2">
        {it.status && <StatusDot status={it.status} className="mt-1.5" />}
        <div className="min-w-0">
          <div className="break-words text-[13px] text-text">{it.title}</div>
          {it.subtitle && <div className="line-clamp-2 break-words text-[12px] text-text-muted">{it.subtitle}</div>}
        </div>
      </div>
      {(it.badge || it.actions) && (
        <div className="flex shrink-0 items-center gap-2">
          {it.badge && (
            <Badge variant="outline" className="shrink-0 font-normal text-text-muted">
              {it.badge}
            </Badge>
          )}
          {it.actions && act && <ItemActions act={act} actions={it.actions} label={it.title} onAsk={onAsk} />}
        </div>
      )}
      {asking?.input && act && <InlineAsk action={asking} act={{ ...act, onCancelAsk: cancelAsk }} label={it.title} />}
      {act?.error && <ErrorAlert error={act.error} onDismiss={act.dismissError} className="col-span-2" />}
    </li>
  )
}

/** A column whose cells are all numbers (or empty) is right-aligned with tabular figures. */
function numericColumn(rows: Record<string, unknown>[], key: string): boolean {
  return rows.some((r) => typeof r[key] === 'number') && rows.every((r) => r[key] === null || r[key] === undefined || typeof r[key] === 'number')
}
function numericKeys(n: NodeOf<'table'>): Set<string> {
  return new Set(n.columns.filter((c) => c.align === 'right' || (c.align !== 'left' && numericColumn(n.rows, c.key))).map((c) => c.key))
}

const CHIP_TONE: Record<string, string> = {
  running: 'bg-info', ok: 'bg-success', done: 'bg-success', approved: 'bg-success', merged: 'bg-success', passing: 'bg-success', success: 'bg-success', granted: 'bg-success',
  failed: 'bg-danger', failing: 'bg-danger', error: 'bg-danger', refused: 'bg-danger', rejected: 'bg-danger',
  pending: 'bg-warning', open: 'bg-warning', waiting: 'bg-warning', blocked: 'bg-warning', review: 'bg-warning',
  pass: 'bg-success', fail: 'bg-danger', requested: 'bg-warning', 'changes requested': 'bg-danger', enabled: 'bg-success', 'granted once': 'bg-success', 'granted for this epic': 'bg-success',
  active: 'bg-success', accepted: 'bg-success', answered: 'bg-success', expiring: 'bg-warning', queued: 'bg-warning', denied: 'bg-danger',
}
/** State words in a `status`/`state` column read as a small chip with a dot (neutral surface, no orange). */
function StateChip({ text }: { text: string }) {
  const tone = CHIP_TONE[text.toLowerCase()] ?? 'bg-text-faint'
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-border px-2 py-px text-[12px] leading-4 text-text-muted">
      <span aria-hidden className={cn('size-1.5 rounded-full', tone)} />
      {text}
    </span>
  )
}

/**
 * A table node. Core folds columns by the table's own width (N11, src/lib/columnFold.ts): what does not fit moves into
 * the row's second line under the first column, so the table never scrolls sideways in a narrow page area.
 */
function TableNodeView({ n, depth }: { n: NodeOf<'table'>; depth: number }) {
  const [ref, width] = useElementWidth<HTMLDivElement>()
  // rowDetail: the one open row (by its key cell); opening another closes it.
  const [openRow, setOpenRow] = useState<string | null>(null)
  const detailId = useId()
  // Still wider than its box after the rule (unbreakable cells): fold one more column until it fits; start over when
  // the width changes.
  const [extra, setExtra] = useState({ width, n: 0 })
  const more = extra.width === width ? extra.n : 0
  const folded = new Set(foldedColumns(n.columns, width, { actions: !!n.rowActions, extra: more }))
  const box = useRef<HTMLDivElement | null>(null)
  // One stable ref for both: an inline callback would detach and re-attach the observer on every commit.
  const attach = useCallback(
    (el: HTMLDivElement | null) => {
      ref(el)
      box.current = el
    },
    [ref],
  )
  useLayoutEffect(() => {
    const scroller = box.current?.querySelector<HTMLElement>('[data-slot="table-container"]')
    if (!scroller || width === 0 || scroller.scrollWidth <= scroller.clientWidth + 1 || more >= n.columns.length) return
    setExtra({ width, n: more + 1 })
  })
  const shown = n.columns.filter((c) => !folded.has(c.key))
  const fold = n.columns.filter((c) => folded.has(c.key))
  const numeric = numericKeys(n)
  return (
    <div ref={attach} data-folded={fold.length || undefined}>
      <Table>
        <TableHeader>
          <TableRow>
            {n.rowDetail && (
              <TableHead className="h-8 w-8 px-1">
                <span className="sr-only">Details</span>
              </TableHead>
            )}
            {shown.map((c) => (
              <TableHead key={c.key} className={cn('h-8 whitespace-normal text-[12px] text-text-muted', numeric.has(c.key) && 'text-right')}>
                {c.label}
              </TableHead>
            ))}
            {n.rowActions && (
              <TableHead className="h-8">
                <span className="sr-only">Actions</span>
              </TableHead>
            )}
          </TableRow>
        </TableHeader>
        <TableBody>
          {stableKeys(n.rows.map((r) => r.id ?? r[n.columns[0].key])).map((k, i) => {
            const row = n.rows[i]
            const cell = n.rowDetail ? row[n.rowDetail.key] : undefined
            const rowKey = typeof cell === 'string' || typeof cell === 'number' ? String(cell) : null
            // The open row is remembered by its stable row key (not the detail key's value), so two rows that share a
            // value still open one at a time.
            const detail =
              n.rowDetail && rowKey !== null && Object.hasOwn(n.rowDetail.nodes, rowKey)
                ? { node: n.rowDetail.nodes[rowKey], open: openRow === k, id: `${detailId}-${i}`, depth, toggle: () => setOpenRow(openRow === k ? null : k) }
                : undefined
            const props = { columns: n.columns, shown, fold, row, numeric, total: !!n.totalRow && i === n.rows.length - 1, detail, hasDetail: !!n.rowDetail }
            return n.rowActions || n.rowOpen ? <ActionDataRow key={k} {...props} rowActions={n.rowActions} rowOpen={n.rowOpen} /> : <DataRowView key={k} {...props} />
          })}
        </TableBody>
      </Table>
    </div>
  )
}

type TableColumn = NodeOf<'table'>['columns'][number]

/** A row that opens (rowDetail): its node, whether it is the open one, and the id of the detail row. */
type RowDetail = { node: unknown; open: boolean; id: string; depth: number; toggle: () => void }
type DataRowProps = { total?: boolean; columns: TableColumn[]; shown: TableColumn[]; fold: TableColumn[]; row: Record<string, unknown>; rowActions?: ItemAction[]; rowOpen?: NodeOf<'table'>['rowOpen']; numeric: Set<string>; detail?: RowDetail; hasDetail?: boolean }

function ActionDataRow(props: DataRowProps) {
  return <DataRowView {...props} act={useAddonAction()} />
}

const TICKET_KEY = /^[A-Z][A-Z0-9]*-\d+$/

function Cell({ column, row, open, act, label }: { column: NodeOf<'table'>['columns'][number]; row: Record<string, unknown>; open?: NodeOf<'table'>['rowOpen']; act?: AddonAction; label: string }) {
  const v = row[column.key]
  const text = cellText(column.key, v)
  if (open && act) {
    return (
      <button type="button" disabled={act.pending || act.blockedFor(open.action)} onClick={() => act.run(open.action, resolveRowArgs(open.args, row), label)} className="text-left font-medium text-text hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring disabled:no-underline">
        {text}
      </button>
    )
  }
  if (column.cell === 'state' && typeof v === 'string' && v !== '') return <StateChip text={text} />
  if (column.cell === 'ticket' && typeof v === 'string' && TICKET_KEY.test(v))
    return (
      <Link to="/ticket/$key" params={{ key: v }} className="whitespace-nowrap font-mono text-[12px] text-text hover:underline">
        {v}
      </Link>
    )
  // A short single token (a key, an id, "#31", a number) never breaks inside; long ones (paths, branches) may (N11).
  if (SHORT_TOKEN.test(text)) return <span className="whitespace-nowrap">{text}</span>
  return <>{text}</>
}

const SHORT_TOKEN = /^\S{1,24}$/

function DataRowView({ columns, shown, fold, row, rowActions, rowOpen, act, numeric, total, detail, hasDetail }: DataRowProps & { act?: AddonAction }) {
  const label = rowLabel(columns[0]?.key, row)
  const span = shown.length + (rowActions ? 1 : 0) + (hasDetail ? 1 : 0)
  return (
    <>
      <TableRow className={cn(total && 'border-t-2 border-border-strong bg-surface-2/60 font-semibold', detail?.open && 'border-b-0 bg-surface-2/60')}>
        {hasDetail && (
          <TableCell className="w-8 px-1 py-1.5">
            {detail && (
              <button
                type="button"
                aria-expanded={detail.open}
                aria-controls={detail.id}
                aria-label={`Details for ${label}`}
                onClick={detail.toggle}
                className="flex size-7 items-center justify-center rounded-md text-text-muted outline-none hover:bg-surface-2 hover:text-text focus-visible:ring-2 focus-visible:ring-ring"
              >
                <ChevronRight aria-hidden className={cn('size-4 transition-transform', detail.open && 'rotate-90')} />
              </button>
            )}
          </TableCell>
        )}
        {shown.map((c, i) => (
          <TableCell key={c.key} className={cn('whitespace-normal break-words py-1.5 text-[13px] [overflow-wrap:anywhere]', numeric.has(c.key) && 'text-right tabular-nums')}>
            <Cell column={c} row={row} open={c === columns[0] ? rowOpen : undefined} act={act} label={label} />
            {i === 0 && fold.length > 0 && (
              <div className="mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[12px] font-normal text-text-muted" data-fold-line>
                {fold.map((f) => (
                  <span key={f.key} className="inline-flex min-w-0 items-center gap-1">
                    <span className="text-text-faint">{f.label}</span>
                    <Cell column={f} row={row} act={act} label={label} />
                  </span>
                ))}
              </div>
            )}
          </TableCell>
        ))}
        {rowActions && act && (
          <TableCell className="whitespace-nowrap py-1.5 text-right">
            <ItemActions act={act} actions={rowActions} row={row} label={label} />
          </TableCell>
        )}
      </TableRow>
      {act?.error && (
        <TableRow className="hover:bg-transparent">
          <TableCell colSpan={span} className="py-1.5">
            <ErrorAlert error={act.error} onDismiss={act.dismissError} />
          </TableCell>
        </TableRow>
      )}
      {detail?.open && (
        <TableRow id={detail.id} className="bg-surface-2/60 hover:bg-surface-2/60">
          <TableCell colSpan={span} className="whitespace-normal px-3 pb-3 pt-1 pl-10">
            <NodeView node={detail.node} depth={detail.depth + 1} />
          </TableCell>
        </TableRow>
      )}
    </>
  )
}

/**
 * Row/item actions that fit: the first non-danger action stays a button, the rest go into a "⋯" menu named
 * "More actions for {title}", danger last and in the danger colour. A single action is just its button. An action
 * with `when` is offered only while that cell of the row says so (core evaluates it).
 */
function ItemActions({ act, actions: all, row, label, onAsk }: { act: AddonAction; actions: ItemAction[]; row?: Record<string, unknown>; label: string; onAsk?: (a: ItemAction) => void }) {
  const { run, pending, blockedFor, dialog } = act
  const actions = all.filter((a) => offered(a.when, row))
  if (actions.length === 0) return <>{dialog}</>
  const start = (a: ItemAction) => (a.input && onAsk ? onAsk(a) : run(a.action, resolveRowArgs(a.args, row), label))
  const lead = actions.length === 1 ? actions[0] : (actions.find((a) => a.primary) ?? actions.find((a) => a.variant !== 'danger') ?? actions[0])
  const more = actions.filter((a) => a !== lead)
  const menu = [...more.filter((a) => a.variant !== 'danger'), ...more.filter((a) => a.variant === 'danger')]
  return (
    <div className="flex shrink-0 items-center gap-1">
      {dialog}
      <Button size="sm" variant={BUTTON_VARIANT[lead.variant]} disabled={pending || blockedFor(lead.action) || !!blockedWhy(lead, row)} title={blockedWhy(lead, row) ?? undefined} onClick={() => start(lead)}>
        {act.pendingAction === lead.action && lead.pendingLabel ? lead.pendingLabel : lead.label}
        {blockedWhy(lead, row) && <span className="sr-only"> ({blockedWhy(lead, row)})</span>}
      </Button>
      {menu.length > 0 && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button size="sm" variant="ghost" aria-label={`More actions for ${label}`} className="px-2">
              <Ellipsis aria-hidden className="size-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {menu.map((a, i) => (
              <DropdownMenuItem key={i} disabled={pending || blockedFor(a.action) || !!blockedWhy(a, row)} onSelect={() => start(a)} className={a.variant === 'danger' ? 'text-danger focus:text-danger' : undefined}>
                {a.label}
                {blockedWhy(a, row) && <span className="ml-2 text-[12px] font-normal text-text-muted">{blockedWhy(a, row)}</span>}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  )
}

const ALERT_TONE = {
  info: 'border-info/40 bg-info-soft',
  success: 'border-success/40 bg-success-soft',
  warn: 'border-warning/40 bg-warning-soft',
  error: 'border-danger/40 bg-danger-soft',
} as const

function AlertNode({ node }: { node: NodeOf<'alert'> }) {
  return (
    <div role="status" className={cn('rounded-md border px-3 py-2', ALERT_TONE[node.tone])}>
      <div className="text-[13px] font-medium text-text">{node.title}</div>
      {node.text && <div className="mt-0.5 text-[12px] text-text-muted">{node.text}</div>}
    </div>
  )
}

function ProgressNode({ node }: { node: NodeOf<'progress'> }) {
  const value = Math.min(node.value, node.max)
  return (
    <div>
      <div className="mb-1 flex justify-between text-[12px] text-text-muted">
        <span>{node.label}</span>
        <span className="font-mono">
          {node.value} / {node.max}
        </span>
      </div>
      <div role="progressbar" aria-label={node.label} aria-valuemin={0} aria-valuemax={node.max} aria-valuenow={value} className="h-1.5 overflow-hidden rounded-full bg-surface-2">
        <div className="h-full rounded-full bg-brand" style={{ width: `${(value / node.max) * 100}%` }} />
      </div>
    </div>
  )
}

/**
 * Only an addon that declares `pty` and holds a current grant may show a terminal; everyone else gets the fallback box.
 * The session id is untrusted: TerminalView resolves it against this addon's own state (this workspace, this viewer).
 */
/** An http(s) address shown in full (it is what the person shares), opening in a new tab, with a Copy button beside it. */
function CopyableLink({ href, label }: { href: string; label: string }) {
  const [copied, setCopied] = useState<'yes' | 'no' | null>(null)
  const copy = () => {
    const done = navigator.clipboard?.writeText(href)
    if (!done) return setCopied('no')
    void done.then(
      () => setCopied('yes'),
      () => setCopied('no'),
    )
  }
  return (
    <span className="inline-flex min-w-0 max-w-full flex-wrap items-center gap-x-2 gap-y-1 text-[13px]">
      <a href={href} target="_blank" rel="noopener noreferrer nofollow" aria-label={`${label}: ${href} (opens in a new tab)`} className="inline-flex min-w-0 items-center gap-1 text-brand hover:underline">
        {/* In full, wrapped and isolated: never cut off, and nothing around it can reorder it (the schema already refuses hidden characters). */}
        <bdi className="whitespace-pre-wrap break-all font-mono text-[12px] [unicode-bidi:isolate]">{visible(href)}</bdi>
        <ExternalLink className="size-3 shrink-0" aria-hidden />
      </a>
      <Button type="button" size="sm" variant="ghost" className="h-7 shrink-0 px-2" aria-label={`Copy ${label}`} onClick={copy}>
        <Copy aria-hidden className="size-3.5" />
        {copied === 'yes' ? 'Copied' : 'Copy'}
      </Button>
      <span role="status" className="sr-only">
        {copied === 'yes' ? 'Address copied' : copied === 'no' ? 'Could not copy the address' : ''}
      </span>
    </span>
  )
}

/** A link to another addon's page: drawn only while that addon is active here (else there is nothing to open). */
function InternalLink({ href, label }: { href: string; label: string }) {
  const { workspace } = useWorkspace()
  const [, , name, page] = href.split('/')
  if (!addonActive(workspace, name)) return null
  return (
    <Link to="/addon/$name/$page" params={{ name, page }} className="inline-flex items-center gap-1 text-[13px] text-brand hover:underline">
      {label}
      <ArrowRight className="size-3" aria-hidden />
    </Link>
  )
}

function TerminalNode({ session }: { session: string }) {
  const { addon, ctx } = useContext(RuntimeCtx)
  const { data } = useAddons()
  const { workspace } = useWorkspace()
  if (!canUsePty(data?.find((a) => a.name === addon), workspace?.addons[addon])) return <AddonUnavailable addon={addon} />
  // No session yet (e.g. the ticket panel before "Open terminal"): nothing to draw.
  if (!session) return null
  return (
    <Suspense fallback={<Skeleton className="h-64 w-full" />}>
      <TerminalView addon={addon} session={session} placement={ctx.ticket ? 'rail' : 'page'} fallback={<AddonUnavailable addon={addon} />} />
    </Suspense>
  )
}
