import { useQuery } from '@tanstack/react-query'
import { toast } from 'sonner'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AddonContributionView } from '@/addon-ui/AddonSlot'
import { useAddons, useSlot } from '@/addon-ui/slots'
import { addonActive } from '@/api/addons'
import type { Workspace } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { queries } from '@/api/queries'

function Loading() {
  return (
    <div aria-busy="true" aria-label="Loading addon settings" className="max-w-3xl space-y-3">
      <Skeleton className="h-10 w-full" />
      <Skeleton className="h-40 w-full" />
    </div>
  )
}

const ONLY_OWNERS_FORM = 'Only owners change settings.'

/** What the person has typed into the form, as one comparable string. */
function formSnapshot(root: HTMLElement | null): string | null {
  const els = Array.from(root?.querySelectorAll<HTMLInputElement>('form input, form select, form textarea') ?? [])
  if (els.length === 0) return null // the form is not there yet: there is nothing to compare with
  return JSON.stringify(els.map((e) => [e.id, e.type === 'checkbox' || e.type === 'radio' ? e.checked : e.value]))
}

/** The drawer's body and footer: the addon's `settings` contribution (a form node) in its frame; read-only unless owner. */
export function AddonSettings({
  name,
  workspace,
  canEdit,
  labelledBy,
  onDirtyChange,
  onClose,
}: {
  name: string
  workspace: Workspace
  canEdit: boolean
  labelledBy: string
  onDirtyChange: (dirty: boolean) => void
  onClose: () => void | Promise<void>
}) {
  const addons = useAddons()
  const contributions = useSlot('settings')
  const pkg = addons.data?.find((a) => a.name === name)
  const c = contributions.find((x) => x.addon === name)
  const active = addonActive(workspace, name)
  // Wait for the saved values: the form is keyed on its data and must not remount under the user's hands.
  const state = useQuery({ ...queries.addonState(workspace.id, name), enabled: active })
  const body = useRef<HTMLDivElement>(null)
  const [dirty, setDirty] = useState(false)
  const saved = JSON.stringify(state.data?.settings ?? null)
  // The values as they were just before the person first touched the form (focus, pointer or key, all before the change); a save (or a reload) puts fresh values in, so start over.
  const baseline = useRef<string | null>(null)
  const touch = useCallback(() => {
    baseline.current ??= formSnapshot(body.current)
  }, [])
  const [save, setSave] = useState({ pending: false, blocked: true })
  // A successful save closes the drawer with a toast; what was just saved is not "unsaved changes" that would stop it.
  const title = pkg?.title ?? name
  const closing = useRef(onClose)
  closing.current = onClose
  const onSaved = useCallback(() => {
    onDirtyChange(false)
    // Toasts of the old page go with the route change, so the confirmation is shown once the list is back.
    void Promise.resolve(closing.current()).then(() => setTimeout(() => toast.success(`${title} settings saved`), 0))
  }, [onDirtyChange, title])
  const formControl = useMemo(() => ({ id: `addon-settings-${name}`, onState: setSave, onSaved }), [name, onSaved])
  useEffect(() => {
    baseline.current = null
    setDirty(false)
    touch()
  }, [saved, touch])
  const check = () => {
    setDirty(baseline.current !== null && formSnapshot(body.current) !== baseline.current)
  }
  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange])

  const failed = (e: unknown) => (
    <p role="alert" className="rounded-md border border-danger/40 bg-danger-soft px-3 py-2 text-[13px]">
      Could not load the {title} settings: {e instanceof Error ? e.message : 'something went wrong'}.
    </p>
  )

  let content
  if (addons.isLoading) content = <Loading />
  else if (addons.error) content = failed(addons.error)
  else if (!pkg) content = <p className="text-sm text-text-muted">No addon named {name} is installed.</p>
  else if (!active) content = <p className="text-sm text-text-muted">Enable {title} to change its settings.</p>
  else if (state.error) content = failed(state.error)
  else if (!state.data) content = <Loading />
  else if (!c) content = <p className="text-sm text-text-muted">{title} has no settings.</p>
  else content = <AddonContributionView c={c} ctx={{ workspace }} readOnly={!canEdit} bare formControl={formControl} />

  return (
    <>
      <div ref={body} role="region" aria-labelledby={labelledBy} className="min-h-0 flex-1 overflow-y-auto px-4" onFocusCapture={touch} onPointerDownCapture={touch} onKeyDownCapture={touch} onInputCapture={check} onChangeCapture={check}>
        {content}
      </div>
      <div className="flex items-center gap-2 border-t border-border p-4">
        <p className="flex-1 text-[12px] text-text-muted">{!canEdit ? ONLY_OWNERS_FORM : dirty ? 'Unsaved changes' : ''}</p>
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        {c && active && (
          <Button type="submit" form={formControl.id} disabled={!canEdit || save.pending || save.blocked}>
            Save
          </Button>
        )}
      </div>
    </>
  )
}
