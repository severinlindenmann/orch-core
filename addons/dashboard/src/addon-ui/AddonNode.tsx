import { createContext, lazy, Suspense, useContext, useId, type ReactNode } from 'react'
import { Ellipsis, ExternalLink, TriangleAlert } from 'lucide-react'
import { useWorkspace } from '@/app/workspace'
import { Badge } from '@/components/ui/badge'
import { STATUS_LABEL } from '@/app/pages/ticket/shared'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { cn } from '@/lib/utils'
import { AddonBadge } from './AddonBadge'
import { AddonChart } from './AddonChart'
import { canUsePty } from './capabilities'
import { FrameNode } from './FrameNode'
import { CodeBlock } from './CodeBlock'
import { MAX_DEPTH, parseNode, type ItemAction, type NodeOf } from './nodes'
import { SafeMarkdown } from './SafeMarkdown'
import { useAddons, type SlotContext } from './slots'
import { useRunAddonAction } from './useRunAddonAction'
import { claimedReason } from './SpawnConfirm'

// rjsf (with ajv) loads on first form, so it stays out of the main bundle.
const ThemedForm = lazy(() => import('./AddonForm'))
// The whole terminal module (xterm included) loads on first use, so the main bundle does not grow.
const TerminalView = lazy(() => import('@/app/terminal/TerminalView'))

interface Runtime {
  addon: string
  ctx: SlotContext
  compact: boolean
  /** Core says the viewer may not change anything: forms, buttons and item actions render disabled. */
  readOnly: boolean
}
const RuntimeCtx = createContext<Runtime>({ addon: '', ctx: {}, compact: false, readOnly: false })

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
export function AddonNode({ node, addon, ctx = {}, compact = false, readOnly = false }: { node: unknown; addon: string; ctx?: SlotContext; compact?: boolean; readOnly?: boolean }) {
  return (
    <RuntimeCtx.Provider value={{ addon, ctx, compact, readOnly }}>
      <NodeView node={node} depth={0} />
    </RuntimeCtx.Provider>
  )
}

function NodeView({ node: raw, depth }: { node: unknown; depth: number }) {
  const { addon } = useContext(RuntimeCtx)
  const parsed = depth > MAX_DEPTH ? ({ ok: false } as const) : parseNode(raw)
  if (!parsed.ok) return <AddonUnavailable addon={addon} />
  const n = parsed.node
  switch (n.type) {
    case 'stack':
      return (
        <div className={cn('flex gap-3', n.direction === 'row' ? 'flex-row [&>*]:min-w-0 [&>*]:flex-1' : 'flex-col')}>
          {n.children.map((c, i) => (
            <NodeView key={i} node={c} depth={depth + 1} />
          ))}
        </div>
      )
    case 'stat':
      return <Stat node={n} />
    case 'kv':
      return (
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-[13px]">
          {n.pairs.map((p, i) => (
            <div key={i} className="contents">
              <dt className="text-text-muted">{p.label}</dt>
              <dd className={cn('text-text', p.mono && 'font-mono text-[12px]')}>{p.value === null || p.value === '' ? '–' : String(p.value)}</dd>
            </div>
          ))}
        </dl>
      )
    case 'list':
      return n.items.length === 0 ? (
        <p className="text-[13px] text-text-faint">{n.empty ?? 'Nothing here.'}</p>
      ) : (
        <ul className="divide-y divide-border">
          {n.items.map((it, i) => (
            <li key={i} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 py-1.5 first:pt-0 last:pb-0">
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
                  {it.actions && <ItemActions actions={it.actions} label={it.title} />}
                </div>
              )}
            </li>
          ))}
        </ul>
      )
    case 'table':
      if (n.rows.length === 0) return <p className="text-[13px] text-text-faint">{n.empty ?? 'Nothing here.'}</p>
      return (
        <Table>
          <TableHeader>
            <TableRow>
              {n.columns.map((c) => (
                <TableHead key={c.key} className="h-8 text-[12px] text-text-muted">
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
            {n.rows.map((r, i) => (
              <TableRow key={i}>
                {n.columns.map((c) => (
                  <TableCell key={c.key} className="whitespace-normal break-words py-1.5 text-[13px]">
                    {cellText(c.key, r[c.key])}
                  </TableCell>
                ))}
                {n.rowActions && (
                  <TableCell className="whitespace-nowrap py-1.5 text-right">
                    <ItemActions actions={n.rowActions} row={r} label={rowLabel(n.columns[0]?.key, r)} />
                  </TableCell>
                )}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )
    case 'markdown':
      return (
        <div className="max-w-[72ch]">
          <SafeMarkdown text={n.text} />
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
    case 'link':
      return (
        <a href={n.href} target="_blank" rel="noopener noreferrer nofollow" className="inline-flex items-center gap-1 text-[13px] text-brand hover:underline">
          {n.label}
          <ExternalLink className="size-3" />
        </a>
      )
  }
}

function Stat({ node }: { node: NodeOf<'stat'> }) {
  const { compact } = useContext(RuntimeCtx)
  const value = node.value === null || node.value === '' ? '–' : node.value
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
      <div className="mt-0.5 text-xl font-semibold tracking-tight text-text">{value}</div>
      {node.hint && <div className="mt-0.5 text-[12px] text-text-faint">{node.hint}</div>}
    </div>
  )
}

/**
 * Runs an action of this addon in the current workspace, through the one action hook (`useRunAddonAction`).
 * `blocked`: no workspace yet, the viewer's role is below the manifest's minRole, or core says read-only (a read-only
 * surface still runs the actions the package declares with minRole 'viewer', e.g. navigation).
 */
function useAddonAction(action?: string): { run: (action: string, extra?: Record<string, unknown>) => void; pending: boolean; blocked: boolean; blockedFor: (action: string) => boolean; dialog: ReactNode; precheck: string | null } {
  const { addon, ctx, readOnly } = useContext(RuntimeCtx)
  const { workspace } = useWorkspace()
  const r = useRunAddonAction(ctx.ticket?.key)
  const blockedFor = (a?: string) => !a || !r.allowed(addon, a) || (readOnly && r.meta(addon, a)?.minRole !== 'viewer')
  // Core's precheck before its start dialog: a claimed ticket gets no second agent (from the ticket, not the addon).
  const precheck =
    action && r.meta(addon, action)?.confirm === 'spawn_agent' && ctx.ticket
      ? claimedReason(ctx.ticket, (id) => workspace?.members.find((m) => m.person === id)?.name ?? id)
      : null
  return { run: (a, extra) => r.run(addon, a, extra), pending: r.pending, blocked: action ? blockedFor(action) : readOnly, blockedFor, dialog: r.dialog, precheck }
}

const BUTTON_VARIANT = { primary: 'default', secondary: 'secondary', ghost: 'ghost', danger: 'destructive' } as const

/** Core's reason an agent cannot start here (claimed ticket), shown at the control. */
function PrecheckAlert({ id, text }: { id?: string; text: string }) {
  return (
    <p id={id} role="alert" className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning-soft px-2.5 py-1.5 text-[12px] text-text">
      <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-warning" aria-hidden />
      {text}
    </p>
  )
}

function ButtonNode({ node }: { node: NodeOf<'button'> }) {
  const { run, pending, blocked, dialog, precheck } = useAddonAction(node.action)
  const reasonId = useId()
  return (
    <div className="space-y-2">
      {precheck && <PrecheckAlert id={reasonId} text={precheck} />}
      <Button
        size="sm"
        variant={BUTTON_VARIANT[node.variant]}
        disabled={pending || blocked || !!precheck}
        aria-describedby={precheck ? reasonId : undefined}
        onClick={() => run(node.action)}
      >
        {node.label}
      </Button>
      {dialog}
    </div>
  )
}

function FormNode({ node }: { node: NodeOf<'form'> }) {
  const { run, pending, blocked: roleBlocked, dialog, precheck } = useAddonAction(node.action)
  // Core's spawn_agent precheck applies to a form that starts an agent as it does to a button.
  const blocked = roleBlocked || !!precheck
  const readOnly = blocked
  return (
    <>
      {dialog}
      {precheck && <PrecheckAlert text={precheck} />}
      <Suspense fallback={<Skeleton className="h-24 w-full" />}>
        <ThemedForm
          disabled={readOnly}
          key={JSON.stringify(node.formData ?? null)}
          schema={node.schema}
          uiSchema={{ ...node.uiSchema, 'ui:submitButtonOptions': { submitText: node.submitLabel ?? 'Save', props: { disabled: pending || blocked } } }}
          formData={node.formData ?? undefined}
          noHtml5Validate
          showErrorList={false}
          onSubmit={({ formData }) => run(node.action, { formData })}
        />
      </Suspense>
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

/**
 * Row/item actions that fit: the first non-danger action stays a button, the rest go into a "⋯" menu named
 * "More actions for {title}", danger last and in the danger colour. A single action is just its button.
 */
function ItemActions({ actions, row, label }: { actions: ItemAction[]; row?: Record<string, unknown>; label: string }) {
  const { run, pending, blockedFor, dialog } = useAddonAction()
  const start = (a: ItemAction) => run(a.action, resolveRowArgs(a.args, row))
  const lead = actions.length === 1 ? actions[0] : (actions.find((a) => a.variant !== 'danger') ?? actions[0])
  const more = actions.filter((a) => a !== lead)
  const menu = [...more.filter((a) => a.variant !== 'danger'), ...more.filter((a) => a.variant === 'danger')]
  return (
    <div className="flex shrink-0 items-center gap-1">
      {dialog}
      <Button size="sm" variant={BUTTON_VARIANT[lead.variant]} disabled={pending || blockedFor(lead.action)} onClick={() => start(lead)}>
        {lead.label}
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
              <DropdownMenuItem key={i} disabled={pending || blockedFor(a.action)} onSelect={() => start(a)} className={a.variant === 'danger' ? 'text-danger focus:text-danger' : undefined}>
                {a.label}
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
function TerminalNode({ session }: { session: string }) {
  const { addon, ctx } = useContext(RuntimeCtx)
  const { data } = useAddons()
  const { workspace } = useWorkspace()
  if (!canUsePty(data?.find((a) => a.name === addon), workspace?.addons[addon])) return <AddonUnavailable addon={addon} />
  return (
    <Suspense fallback={<Skeleton className="h-64 w-full" />}>
      <TerminalView addon={addon} session={session} placement={ctx.ticket ? 'rail' : 'page'} fallback={<AddonUnavailable addon={addon} />} />
    </Suspense>
  )
}
