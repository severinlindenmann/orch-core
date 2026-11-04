// An add-on's settings in a right-hand side panel (#1237 pick 1.2a): the list stays in view. The controls come from the
// schema the add-on declares: a fixed choice is a picker, a boolean a switch, a number a stepper with its range, and
// only an open-ended text setting is an input. The Save press prepares the One click review; its armed Approve signs.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { SchemaProp } from '@/api/addOnSwitches'
import { api } from '@/api/client'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Switch } from '@/components/ui/switch'
import type { Workspace } from '@/api/types'
import { LoadFailed } from '@/components/LoadFailed'
import { useSettingsHandoff } from '../settings/host/useSettingsHandoff'

type Values = Record<string, unknown>
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)
const label = (key: string, p: SchemaProp) => p.title ?? key.replace(/[-_]/g, ' ').replace(/^./, (c) => c.toUpperCase())

function Field({ k, p, value, onChange }: { k: string; p: SchemaProp; value: unknown; onChange: (v: unknown) => void }) {
  const id = `setting-${k}`
  const name = label(k, p)
  let control
  if (p.enum) {
    control = (
      <div role="radiogroup" aria-label={name} className="flex flex-wrap gap-1">
        {p.enum.map((o) => (
          <Button key={String(o)} type="button" size="sm" role="radio" aria-checked={value === o} variant={value === o ? 'secondary' : 'ghost'} className="border border-border" onClick={() => onChange(o)}>
            {String(o)}
          </Button>
        ))}
      </div>
    )
  } else if (p.type === 'boolean') {
    control = <Switch id={id} aria-label={name} checked={value === true} onCheckedChange={onChange} />
  } else if (p.type === 'integer' || p.type === 'number') {
    const step = p.multipleOf ?? 1
    const n = typeof value === 'number' ? value : (p.minimum ?? 0)
    const clamp = (x: number) => Math.min(p.maximum ?? Infinity, Math.max(p.minimum ?? -Infinity, x))
    control = (
      <div className="flex items-center gap-2">
        <Button type="button" size="sm" variant="secondary" aria-label={`Less ${name}`} onClick={() => onChange(clamp(n - step))}>
          −
        </Button>
        <span aria-label={name} role="spinbutton" aria-valuenow={n} aria-valuemin={p.minimum} aria-valuemax={p.maximum} className="min-w-12 text-center font-mono">
          {n}
        </span>
        <Button type="button" size="sm" variant="secondary" aria-label={`More ${name}`} onClick={() => onChange(clamp(n + step))}>
          +
        </Button>
        {(p.minimum !== undefined || p.maximum !== undefined) && (
          <span className="text-[12px] text-text-muted">
            {p.minimum ?? '…'}–{p.maximum ?? '…'}
            {p.multipleOf ? `, steps of ${p.multipleOf}` : ''}
          </span>
        )}
      </div>
    )
  } else if (p.type === 'string') {
    control = <Input id={id} aria-label={name} value={typeof value === 'string' ? value : ''} onChange={(e) => onChange(e.target.value)} />
  } else {
    control = <p className="text-[12px] text-text-muted">This setting cannot be edited here yet.</p>
  }
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="text-[13px] font-medium">
        <Inline>{name}</Inline>
      </label>
      {p.description && <p className="text-[12px] text-text-muted"><Inline>{p.description}</Inline></p>}
      {control}
    </div>
  )
}

export function SettingsSheet({ workspace, name, onClose }: { workspace: Workspace; name: string; onClose: () => void }) {
  const ws = workspace.id
  const qc = useQueryClient()
  const data = useQuery({ queryKey: ['add-on-settings', ws, name], queryFn: () => api.addonSettings(ws, name), retry: false })
  const [edit, setEdit] = useState<Values | null>(null)
  const { ask, dialog } = useSettingsHandoff(ws, async () => {
    await qc.invalidateQueries({ queryKey: ['add-on-settings', ws, name] })
    setEdit(null)
  })
  const props = data.data?.schema.properties ?? {}
  const base: Values = { ...Object.fromEntries(Object.entries(props).filter(([, p]) => p.default !== undefined).map(([k, p]) => [k, p.default])), ...(data.data?.value ?? {}) }
  const now = edit ?? base
  const dirty = !same(now, base)
  const title = data.data?.title || name
  const save = () =>
    ask({
      title: `Save ${title} settings`,
      action: { action: 'addon_settings', name, value: now },


      confirmLabel: 'Approve and save',
      doneToast: `${title} settings saved`,
      summary: <p className="text-[13px] text-text-muted">Saves the settings of {title} for this workspace.</p>,
    })
  return (
    <Sheet open onOpenChange={(o) => !o && onClose()}>
      <SheetContent
        side="right"
        data-testid="settings-sheet"
        // the signing popover sits outside the panel: reaching it is not "leaving" the panel
        onInteractOutside={(e) => (e.target as Element | null)?.closest?.('[data-handoff-dialog]') && e.preventDefault()}
        onFocusOutside={(e) => (e.target as Element | null)?.closest?.('[data-handoff-dialog]') && e.preventDefault()} className="w-[min(32rem,100vw)] gap-4 overflow-y-auto border-border bg-surface p-4">
        <SheetHeader>
          <SheetTitle><Inline>{title}</Inline> settings</SheetTitle>
          <SheetDescription>Saving is a change you approve.</SheetDescription>
        </SheetHeader>
        {data.isError ? (
          <LoadFailed what="settings" onRetry={() => void data.refetch()} />
        ) : !data.data ? (
          <p className="text-[13px] text-text-muted">Reading the settings…</p>
        ) : (
          <div className="space-y-4">
            {Object.entries(props).map(([k, p]) => (
              <Field key={k} k={k} p={p} value={now[k]} onChange={(v) => setEdit({ ...now, [k]: v })} />
            ))}
            {!Object.keys(props).length && <p className="text-[13px] text-text-muted">This add-on has nothing to set here.</p>}
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={onClose}>
                Cancel
              </Button>
              <Button disabled={!dirty} onClick={save}>
                Save
              </Button>
            </div>
          </div>
        )}
        {dialog}
      </SheetContent>
    </Sheet>
  )
}
