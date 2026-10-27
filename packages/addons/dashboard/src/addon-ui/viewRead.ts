// Named reads of declarative views (design D7, `addon.view`). A node names a read ("wiki.tree"); it never carries a URL.
// core answers it from the dashboard server's registry (server/src/orch_dashboard/addon_reads.py), read only. The
// answer is validated here again with the schemas below: a wrong shape is an error state, never a half-drawn page.
import { useQuery } from '@tanstack/react-query'
import { z } from 'zod'
import { call } from '@/api/runs'
import { useWorkspace } from '@/app/workspace'
import { READ_NAME } from './nodes'


const nodeId = z.string().min(1).max(200).regex(/^[A-Za-z0-9_.:/-]+$/)
const line = z.string().max(400)

/** A node of a `tree` read. `children: true` means a further read with `parent=<id>` lists its children (lazily). */
export const treeEntry = z.object({ id: nodeId, label: line, hint: line.optional(), badge: z.string().max(60).optional(), children: z.boolean().optional() })
export type TreeEntry = z.output<typeof treeEntry>
export const treeResult = z.object({
  /** `ok`, or why there is nothing to list (`no_folder`...): `message` then says it in words. */
  state: z.string().max(40),
  message: line.nullish(),
  nodes: z.array(treeEntry).max(500),
  total: z.number().int().min(0).max(1_000_000),
  cut: z.boolean(),
  /** One plain action the viewer may take when there is nothing to show (an add-on action id). */
  offer: z.object({ label: z.string().max(60), action: z.string().regex(/^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}$/) }).optional(),
})
export type TreeResult = z.output<typeof treeResult>

export const searchResult = z.object({
  results: z.array(z.object({ id: nodeId, label: line, snippet: line.nullish() })).max(100),
  total: z.number().int().min(0).max(1_000_000),
  cut: z.boolean(),
})
export type SearchResult = z.output<typeof searchResult>

/** A page read: only text fields; which two names the title and the text is the node's `detail` (see nodes.ts). */
export const detailResult = z.record(z.string(), z.union([z.string().max(300_000), z.null()])).superRefine((o, ctx) => {
  if (Object.keys(o).length > 16) ctx.addIssue({ code: 'custom', message: 'too many fields' })
})
export type DetailResult = z.output<typeof detailResult>

type Args = Record<string, string>

/** The query key sits under the add-on's state key, so an add-on action that changed something refetches its reads. */
export const viewReadKey = (ws: string | undefined, addon: string, name: string, args: Args) => ['addon-state', ws, addon, 'view', name, args]

export function useViewRead<S extends z.ZodType>(addon: string, name: string, args: Args, schema: S, enabled = true) {
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const own = name.startsWith(`${addon}.`) && READ_NAME.test(name)
  return useQuery({
    queryKey: viewReadKey(ws, addon, name, args),
    enabled: enabled && !!ws && own,
    retry: false,
    staleTime: 10_000,
    queryFn: async (): Promise<z.output<S>> => {
      const q = new URLSearchParams(args).toString()
      const raw = await call<unknown>('GET', `/api/w/${encodeURIComponent(ws as string)}/view/${name}${q ? `?${q}` : ''}`)
      return schema.parse(raw)
    },
  })
}
