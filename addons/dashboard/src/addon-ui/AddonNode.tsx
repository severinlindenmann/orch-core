import { createContext, useContext } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { withTheme } from '@rjsf/core'
import validator from '@rjsf/validator-ajv8'
import { ExternalLink, TriangleAlert } from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { cn } from '@/lib/utils'
import { AddonBadge } from './AddonBadge'
import { AddonChart } from './AddonChart'
import { CodeBlock } from './CodeBlock'
import { MAX_DEPTH, parseNode, type NodeOf } from './nodes'
import { darkTheme } from './rjsfTheme'
import { SafeMarkdown } from './SafeMarkdown'
import type { SlotContext } from './slots'

const ThemedForm = withTheme(darkTheme)

interface Runtime {
  addon: string
  ctx: SlotContext
  compact: boolean
}
const RuntimeCtx = createContext<Runtime>({ addon: '', ctx: {}, compact: false })

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
export function AddonNode({ node, addon, ctx = {}, compact = false }: { node: unknown; addon: string; ctx?: SlotContext; compact?: boolean }) {
  return (
    <RuntimeCtx.Provider value={{ addon, ctx, compact }}>
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
              <div className="min-w-0 flex-1">
                <div className="truncate text-[13px] text-text">{it.title}</div>
                {it.subtitle && <div className="truncate text-[12px] text-text-faint">{it.subtitle}</div>}
              </div>
              {it.badge && (
                <Badge variant="outline" className="shrink-0 font-normal text-text-muted">
                  {it.badge}
                </Badge>
              )}
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

function useAddonAction(): { run: (action: string, extra?: Record<string, unknown>) => void; pending: boolean } {
  const { addon, ctx } = useContext(RuntimeCtx)
  const qc = useQueryClient()
  const m = useMutation({
    mutationFn: ({ action, extra }: { action: string; extra?: Record<string, unknown> }) =>
      api.runAddonAction(addon, action, { ...extra, ...(ctx.ticket ? { ticket: ctx.ticket.key } : {}) }),
    onSuccess: (res) => {
      toast.success(res.message)
      if (res.changed) void qc.invalidateQueries()
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : 'Action failed'),
  })
  return { run: (action, extra) => m.mutate({ action, extra }), pending: m.isPending }
}

const BUTTON_VARIANT = { primary: 'default', secondary: 'secondary', ghost: 'ghost', danger: 'destructive' } as const

function ButtonNode({ node }: { node: NodeOf<'button'> }) {
  const { run, pending } = useAddonAction()
  return (
    <div>
      <Button size="sm" variant={BUTTON_VARIANT[node.variant]} disabled={pending} onClick={() => run(node.action)}>
        {node.label}
      </Button>
    </div>
  )
}

function FormNode({ node }: { node: NodeOf<'form'> }) {
  const { run, pending } = useAddonAction()
  return (
    <ThemedForm
      key={JSON.stringify(node.formData ?? null)}
      schema={node.schema}
      uiSchema={{ ...node.uiSchema, 'ui:submitButtonOptions': { submitText: node.submitLabel ?? 'Save', props: { disabled: pending } } }}
      formData={node.formData ?? undefined}
      validator={validator}
      noHtml5Validate
      showErrorList={false}
      onSubmit={({ formData }) => run(node.action, { formData })}
    />
  )
}

