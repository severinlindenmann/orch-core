import { useQuery } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'
import { AddonContributionView } from '@/addon-ui/AddonSlot'
import { useAddons, useSlot } from '@/addon-ui/slots'
import { addonActive } from '@/api/addons'
import { api } from '@/api/client'
import type { Workspace } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'

function Loading() {
  return (
    <div aria-busy="true" aria-label="Loading addon settings" className="max-w-3xl space-y-3">
      <Skeleton className="h-10 w-full" />
      <Skeleton className="h-40 w-full" />
    </div>
  )
}

const ONLY_OWNERS_FORM = 'Only owners change settings.'

/**
 * The form's own Save button lives inside the addon's form node. In the drawer it is hidden and mirrored by the
 * footer's Save, so the drawer ends in Save / Cancel. Submitting still goes through the node (same action, same path).
 */
function useExternalSubmit(body: React.RefObject<HTMLDivElement | null>, onForm: () => void) {
  const [button, setButton] = useState<{ present: boolean; disabled: boolean }>({ present: false, disabled: true })
  useEffect(() => {
    const el = body.current
    if (!el) return
    const sync = () => {
      const b = el.querySelector<HTMLButtonElement>('form button[type="submit"]')
      if (b) {
        b.hidden = true
        onForm()
      }
      setButton((prev) => {
        const next = { present: !!b, disabled: b?.disabled ?? true }
        return prev.present === next.present && prev.disabled === next.disabled ? prev : next
      })
    }
    sync()
    const mo = new MutationObserver(sync)
    mo.observe(el, { childList: true, subtree: true, attributes: true, attributeFilter: ['disabled'] })
    return () => mo.disconnect()
  }, [body, onForm])
  const submit = () => body.current?.querySelector<HTMLFormElement>('form')?.requestSubmit()
  return { ...button, submit }
}

/** What the person has typed into the form, as one comparable string. */
function formSnapshot(root: HTMLElement | null): string {
  const els = Array.from(root?.querySelectorAll<HTMLInputElement>('form input, form select, form textarea') ?? [])
  return JSON.stringify(els.map((e) => [e.id, e.type === 'checkbox' || e.type === 'radio' ? e.checked : e.value]))
}

/** The drawer's body and footer: the addon's `settings` contribution (a form node) in its frame; read-only unless owner. */
export function AddonSettings({
  name,
  workspace,
  canEdit,
  onDirtyChange,
  onClose,
}: {
  name: string
  workspace: Workspace
  canEdit: boolean
  onDirtyChange: (dirty: boolean) => void
  onClose: () => void
}) {
  const addons = useAddons()
  const contributions = useSlot('settings')
  const pkg = addons.data?.find((a) => a.name === name)
  const c = contributions.find((x) => x.addon === name)
  const active = addonActive(workspace, name)
  // Wait for the saved values: the form is keyed on its data and must not remount under the user's hands.
  const state = useQuery({
    queryKey: ['addon-state', workspace.id, name],
    queryFn: () => api.getAddonState(workspace.id, name),
    enabled: active,
    retry: false,
  })
  const body = useRef<HTMLDivElement>(null)
  const [dirty, setDirty] = useState(false)
  const saved = JSON.stringify(state.data?.settings ?? null)
  // The values as they were when the person first touched the form; a save (or a reload) puts fresh values in, so start over.
  const baseline = useRef<string | null>(null)
  const touch = useCallback(() => {
    baseline.current ??= formSnapshot(body.current)
  }, [])
  const submit = useExternalSubmit(body, touch)
  useEffect(() => {
    baseline.current = null
    setDirty(false)
    touch()
  }, [saved, touch])
  const check = () => setDirty(baseline.current !== null && formSnapshot(body.current) !== baseline.current)
  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange])

  const title = pkg?.title ?? name
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
  else content = <AddonContributionView c={c} ctx={{ workspace }} readOnly={!canEdit} bare />

  return (
    <>
      <div ref={body} className="min-h-0 flex-1 overflow-y-auto px-4" onFocusCapture={touch} onInputCapture={check} onChangeCapture={check}>
        {content}
      </div>
      <div className="flex items-center gap-2 border-t border-border p-4">
        <p className="flex-1 text-[12px] text-text-muted">{!canEdit ? ONLY_OWNERS_FORM : dirty ? 'Unsaved changes' : ''}</p>
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        {submit.present && (
          <Button onClick={submit.submit} disabled={!canEdit || submit.disabled}>
            Save
          </Button>
        )}
      </div>
    </>
  )
}
