import { createContext, useContext } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { withTheme } from '@rjsf/core'
import validator from '@rjsf/validator-ajv8'
import { ExternalLink, TriangleAlert } from 'lucide-react'
import { toast } from 'sonner'
import { toastApiError } from '@/app/toast'
import { api } from '@/api/client'
import { useWorkspace } from '@/app/workspace'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { cn } from '@/lib/utils'
import { AddonBadge } from './AddonBadge'
import { AddonChart } from './AddonChart'
import { AddonFrame } from './AddonFrame'
import { openResultUrl, withoutReservedKeys } from './actionRuntime'
import { canUsePty } from './capabilities'
import { FrameNode } from './FrameNode'
import { CodeBlock } from './CodeBlock'
import { MAX_DEPTH, parseNode, type ItemAction, type NodeOf } from './nodes'
import { darkTheme } from './rjsfTheme'
import { SafeMarkdown } from './SafeMarkdown'
import { useAddons, type SlotContext } from './slots'

const ThemedForm = withTheme(darkTheme)

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
            <li key={i} className="flex items-center gap-2 py-1.5 first:pt-0 last:pb-0">
              {it.status && <StatusDot status={it.status} />}
              <div className="min-w-0 flex-1">
                <div className="truncate text-[13px] text-text">{it.title}</div>
                {it.subtitle && <div className="truncate text-[12px] text-text-faint">{it.subtitle}</div>}
              </div>
              {it.badge && (
                <Badge variant="outline" className="shrink-0 font-normal text-text-muted">
                  {it.badge}
                </Badge>
              )}
              {it.actions && <ItemActions actions={it.actions} />}
            </li>
          ))}
        </ul>
      )
    case 'table':
      return (
        <Table>
          <TableHeader>
            <TableRow>
              {n.columns.map((c) => (
                <TableHead key={c.key} className="h-8 text-[12px] text-text-muted">
                  {c.label}
                </TableHead>
              ))}
              {n.rowActions && <TableHead className="h-8" />}
            </TableRow>
          </TableHeader>
          <TableBody>
            {n.rows.map((r, i) => (
              <TableRow key={i}>
                {n.columns.map((c) => (
                  <TableCell key={c.key} className="py-1.5 text-[13px]">
                    {r[c.key] === null || r[c.key] === undefined ? '–' : String(r[c.key])}
                  </TableCell>
                ))}
                {n.rowActions && (
                  <TableCell className="py-1.5 text-right">
                    <ItemActions actions={n.rowActions} row={r} />
                  </TableCell>
                )}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )
    case 'markdown':
      return <SafeMarkdown text={n.text} />
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
        <AddonFrame addon={addon} title={n.title}>
          <FrameNode node={n} fallback={<AddonUnavailable addon={addon} />} />
        </AddonFrame>
      )
    case 'terminal':
      return <TerminalNode />
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
 * Is this action enabled for the viewer? A read-only viewer (core says so) may still run the actions the package
 * declares with minRole 'viewer' (navigation). The server stays the authority; this only decides what looks clickable.
 */
function useActionAllowed(): (action?: string) => boolean {
  const { addon, readOnly } = useContext(RuntimeCtx)
  const { data } = useAddons()
  const actions = data?.find((a) => a.name === addon)?.actions
  return (action) => !readOnly || (!!action && actions?.[action]?.minRole === 'viewer')
}

/** Runs an action of this addon in the current workspace. `blocked`: no workspace yet, or core says read-only. */
function useAddonAction(action?: string): { run: (action: string, extra?: Record<string, unknown>) => void; pending: boolean; blocked: boolean; blockedFor: (action: string) => boolean } {
  const { addon, ctx } = useContext(RuntimeCtx)
  const allowed = useActionAllowed()
  const qc = useQueryClient()
  const { workspace } = useWorkspace()
  const m = useMutation({
    mutationFn: ({ action, extra }: { action: string; extra?: Record<string, unknown> }) => {
      if (!workspace) throw new Error('No workspace')
      return api.runAddonAction(workspace.id, addon, action, {
        ...withoutReservedKeys(extra),
        ...(ctx.ticket ? { ticket: ctx.ticket.key } : {}),
      })
    },
    onSuccess: (res) => {
      toast.success(res.message)
      openResultUrl(res)
      void qc.invalidateQueries({ queryKey: ['addon-state'] })
      void qc.invalidateQueries({ queryKey: ['ticket'] })
      void qc.invalidateQueries({ queryKey: ['today'] })
      if (res.changed) void qc.invalidateQueries()
    },
    onError: (err) => toastApiError(err, 'Action failed'),
  })
  return { run: (action, extra) => m.mutate({ action, extra }), pending: m.isPending, blocked: !workspace || !allowed(action), blockedFor: (a) => !workspace || !allowed(a) }
}

const BUTTON_VARIANT = { primary: 'default', secondary: 'secondary', ghost: 'ghost', danger: 'destructive' } as const

function ButtonNode({ node }: { node: NodeOf<'button'> }) {
  const { run, pending, blocked } = useAddonAction(node.action)
  return (
    <div>
      <Button size="sm" variant={BUTTON_VARIANT[node.variant]} disabled={pending || blocked} onClick={() => run(node.action)}>
        {node.label}
      </Button>
    </div>
  )
}

function FormNode({ node }: { node: NodeOf<'form'> }) {
  const { run, pending, blocked } = useAddonAction(node.action)
  const readOnly = !useActionAllowed()(node.action)
  return (
    <ThemedForm
      disabled={readOnly}
      key={JSON.stringify(node.formData ?? null)}
      schema={node.schema}
      uiSchema={{ ...node.uiSchema, 'ui:submitButtonOptions': { submitText: node.submitLabel ?? 'Save', props: { disabled: pending || blocked } } }}
      formData={node.formData ?? undefined}
      validator={validator}
      noHtml5Validate
      showErrorList={false}
      onSubmit={({ formData }) => run(node.action, { formData })}
    />
  )
}


const STATUS_DOT = { ok: 'bg-success', warn: 'bg-warning', error: 'bg-danger', idle: 'bg-text-faint', running: 'bg-info animate-pulse' } as const

function StatusDot({ status }: { status: keyof typeof STATUS_DOT }) {
  return <span role="img" aria-label={status} className={cn('size-2 shrink-0 rounded-full', STATUS_DOT[status])} />
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

function ItemActions({ actions, row }: { actions: ItemAction[]; row?: Record<string, unknown> }) {
  const { run, pending, blockedFor } = useAddonAction()
  return (
    <div className="flex shrink-0 items-center gap-1">
      {actions.map((a, i) => (
        <Button key={i} size="sm" variant={BUTTON_VARIANT[a.variant]} disabled={pending || blockedFor(a.action)} onClick={() => run(a.action, resolveRowArgs(a.args, row))}>
          {a.label}
        </Button>
      ))}
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

/** Only an addon that declares `pty` and holds a current grant may show a terminal; everyone else gets the fallback box. */
function TerminalNode() {
  const { addon } = useContext(RuntimeCtx)
  const { data } = useAddons()
  const { workspace } = useWorkspace()
  if (!canUsePty(data?.find((a) => a.name === addon), workspace?.addons[addon])) return <AddonUnavailable addon={addon} />
  // TODO(Task 21): render core's <TerminalView session=…/> here.
  return (
    <div role="alert" className="rounded-md border border-border bg-bg px-3 py-2 text-[13px] text-text-muted">
      Terminal sessions arrive with the terminals addon
    </div>
  )
}
