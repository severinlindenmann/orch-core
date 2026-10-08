import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { ApiError, type SavedView } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { cn } from '@/lib/utils'
import { onSaveViewRequest, takeSaveViewRequest } from './saveViewRequest'
import type { TicketsSearch } from './search'

/** Canonical form for comparing filters: no empty values, lists sorted. */
const norm = (p: object) =>
  JSON.stringify(
    Object.entries(p)
      .filter(([k, v]) => v !== undefined && v !== '' && !(k === 'sort' && v === 'updated'))
      .map(([k, v]): [string, unknown] => [k, Array.isArray(v) ? [...v].sort() : v])
      .sort(([a], [b]) => a.localeCompare(b)),
  )

const tab = (on: boolean) =>
  cn(
    'inline-flex h-7 items-center rounded-md border px-2.5 text-[12px] outline-none focus-visible:ring-2 focus-visible:ring-brand',
    on ? 'border-brand bg-brand-soft text-brand' : 'border-transparent text-text-muted hover:bg-accent hover:text-text',
  )

export function SavedViews({
  wsId,
  search,
  me,
  canShare,
  onApply,
  onClear,
}: {
  wsId: string
  search: TicketsSearch
  me: string | undefined
  canShare: boolean
  onApply: (params: TicketsSearch) => void
  onClear: () => void
}) {
  const qc = useQueryClient()
  const { data: views = [] } = useQuery({ queryKey: ['views', wsId], queryFn: () => api.listViews(wsId) })
  const [activeId, setActiveId] = useState<string | null>(null)
  const [dialog, setDialog] = useState<{ name: string; replace?: SavedView } | null>(null)
  const [shared, setShared] = useState(false)

  const current = norm(search)
  const active = views.find((v) => v.id === activeId) ?? (activeId ? undefined : views.find((v) => norm(v.params) === current))
  const modified = !!active && norm(active.params) !== current

  const fail = (err: unknown) => toast.error(err instanceof ApiError ? err.message : 'Request failed')
  const save = useMutation({
    mutationFn: async ({ name, shared: sh, replace }: { name: string; shared: boolean; replace?: SavedView }) => {
      const v = await api.saveView(wsId, { name, shared: sh, params: search })
      if (replace) await api.deleteView(wsId, replace.id)
      return v
    },
    onSuccess: (v) => {
      setActiveId(v.id)
      setDialog(null)
      void qc.invalidateQueries({ queryKey: ['views', wsId] })
    },
    onError: fail,
  })
  const del = useMutation({
    mutationFn: (v: SavedView) => api.deleteView(wsId, v.id),
    onSuccess: () => {
      setActiveId(null)
      void qc.invalidateQueries({ queryKey: ['views', wsId] })
    },
    onError: fail,
  })

  const openDialog = (name: string, replace?: SavedView) => {
    setShared(replace?.shared ?? false)
    setDialog({ name, replace })
  }
  const nothingActive = !active

  // "Save view…" from the command palette.
  useEffect(() => {
    if (takeSaveViewRequest()) openDialog('')
    return onSaveViewRequest(() => openDialog(''))
  }, [])

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <div role="tablist" aria-label="Saved views" className="flex flex-wrap items-center gap-1">
        <button
          role="tab"
          type="button"
          aria-selected={nothingActive}
          className={tab(nothingActive)}
          onClick={() => {
            setActiveId(null)
            onClear()
          }}
        >
          All tickets
        </button>
        {views.map((v) => (
          <button
            key={v.id}
            role="tab"
            type="button"
            aria-selected={active?.id === v.id}
            title={v.shared ? 'Shared with the workspace' : 'Personal'}
            className={tab(active?.id === v.id)}
            onClick={() => {
              setActiveId(v.id)
              onApply(v.params)
            }}
          >
            {v.name}
          </button>
        ))}
      </div>
      <Button variant="ghost" size="sm" className="h-7 text-[12px] text-text-muted" onClick={() => openDialog('')}>
        Save view…
      </Button>
      {active && active.owner === me && !modified && (
        <Button variant="ghost" size="sm" className="h-7 gap-1 text-[12px] text-text-muted" aria-label={`Delete view ${active.name}`} onClick={() => del.mutate(active)}>
          <Trash2 className="size-3.5" aria-hidden />
        </Button>
      )}
      {modified && (
        <span className="inline-flex items-center gap-1 text-[12px] text-text-muted" role="status">
          Modified ·
          <Button variant="link" size="sm" className="h-6 px-1 text-[12px]" onClick={() => openDialog(active.owner === me ? active.name : `${active.name} (copy)`, active.owner === me ? active : undefined)}>
            Save
          </Button>
          ·
          <Button variant="link" size="sm" className="h-6 px-1 text-[12px]" onClick={() => onApply(active.params)}>
            Revert
          </Button>
        </span>
      )}
      <Dialog open={!!dialog} onOpenChange={(o) => !o && setDialog(null)}>
        <DialogContent>
          <form
            onSubmit={(e) => {
              e.preventDefault()
              if (dialog && dialog.name.trim()) save.mutate({ name: dialog.name.trim(), shared: canShare && shared, replace: dialog.replace })
            }}
          >
            <DialogHeader>
              <DialogTitle>Save view</DialogTitle>
              <DialogDescription>Saves the current filters under a name.</DialogDescription>
            </DialogHeader>
            <div className="my-4 flex flex-col gap-3">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="view-name">Name</Label>
                <Input id="view-name" value={dialog?.name ?? ''} onChange={(e) => setDialog((d) => d && { ...d, name: e.target.value })} autoFocus />
              </div>
              <div className="flex items-center gap-2">
                <Checkbox id="view-shared" checked={canShare && shared} disabled={!canShare} onCheckedChange={(c) => setShared(c === true)} />
                <Label htmlFor="view-shared">Share with the workspace</Label>
                {!canShare && <span className="text-[11px] text-text-faint">Viewers save personal views only.</span>}
              </div>
            </div>
            <DialogFooter>
              <Button type="button" variant="ghost" onClick={() => setDialog(null)}>
                Cancel
              </Button>
              <Button type="submit" disabled={!dialog?.name.trim() || save.isPending}>
                Save
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  )
}
