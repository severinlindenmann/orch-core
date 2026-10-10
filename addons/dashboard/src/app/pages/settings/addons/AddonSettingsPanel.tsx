import { useBlocker, useNavigate } from '@tanstack/react-router'
import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { AddonBadge } from '@/addon-ui/AddonBadge'
import { useAddons } from '@/addon-ui/slots'
import type { Workspace } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { useSwitchGuard } from '@/app/workspace'

// The form (rjsf through AddonNode) loads when the panel first opens.
const AddonSettings = lazy(() => import('../AddonSettings').then((m) => ({ default: m.AddonSettings })))

/** The id the row's Settings button carries, so closing the panel can put focus back on it. */
export const settingsButtonAttr = 'data-settings-for'
/** Where focus goes when the row is not there (after a deep link to an addon without a row). */
export const addonsHeadingAttr = 'data-addons-heading'

// The route change remounts the Addons list, so the row's Settings button is a new element: which addon's button
// should take focus there is remembered here (set by whoever navigates, taken once by the row that mounts).
let focusSettingsOf: string | null = null
export const requestSettingsFocus = (name: string | null) => {
  focusSettingsOf = name
  // A request that no row takes soon (the list was not drawn again) lapses, so it cannot steal focus on a later visit.
  if (name) setTimeout(() => focusSettingsOf === name && (focusSettingsOf = null), 2000)
}
export function takeSettingsFocus(name: string): boolean {
  if (focusSettingsOf !== name) return false
  focusSettingsOf = null
  return true
}

/**
 * One addon's settings form as an accordion panel beneath its row in the Addons table (route /settings/addon/<name>);
 * one panel is open at a time because the route names one addon. Unsaved changes are never lost silently: every way
 * out (Cancel, Esc, another row's Settings, Back, the palette, a link, a workspace switch) asks first. Focus goes
 * back to the row's Settings button; a deep link scrolls the panel into view.
 */
export function AddonSettingsPanel({ name, workspace, canEdit }: { name: string; workspace: Workspace; canEdit: boolean }) {
  const addons = useAddons()
  const navigate = useNavigate()
  const title = addons.data?.find((a) => a.name === name)?.title ?? name
  const titleId = `addon-settings-title-${name}`
  const dirty = useRef(false)
  const [asking, setAsking] = useState<{ keep: () => void; discard: () => void } | null>(null)
  const onDirtyChange = useCallback((d: boolean) => void (dirty.current = d), [])

  // Route changes away from the drawer (Back, links, palette, shortcuts, Cancel itself) while there is something unsaved.
  const blocker = useBlocker({ shouldBlockFn: () => dirty.current, withResolver: true, enableBeforeUnload: false })
  useEffect(() => {
    if (blocker.status === 'blocked') setAsking({ keep: blocker.reset, discard: blocker.proceed })
  }, [blocker.status, blocker.reset, blocker.proceed])
  // A workspace switch happens by state, not by route: ask first as well.
  const guard = useCallback(
    (proceed: () => void) => {
      if (!dirty.current) return proceed()
      setAsking({
        keep: () => {},
        discard: () => {
          dirty.current = false
          proceed()
        },
      })
    },
    [],
  )
  useSwitchGuard(guard)

  const panel = useRef<HTMLDivElement>(null)
  useEffect(() => {
    panel.current?.scrollIntoView?.({ block: 'nearest' })
  }, [name])
  const close = () => {
    requestSettingsFocus(name) // the row's Settings button takes focus once the list is back (not when the question holds it)
    return navigate({ to: '/settings/$tab', params: { tab: 'addons' } })
  }
  const answer = (f: () => void) => {
    setAsking(null)
    f()
  }

  const keep = () =>
    answer(() => {
      requestSettingsFocus(null)
      asking?.keep()
    })

  return (
    <>
      <div
        ref={panel}
        role="group"
        id={`addon-settings-panel-${name}`}
        aria-labelledby={titleId}
        className="rounded-md border border-border bg-bg py-3"
        onKeyDown={(e) => {
          // Esc that belongs to a popup of the form (portals bubble through React) is not ours.
          if (e.key === 'Escape' && !e.defaultPrevented && panel.current?.contains(e.target as Node)) void close()
        }}
      >
        <h3 className="mb-3 flex items-center gap-2 px-4 text-sm font-semibold">
          <AddonBadge name={name} />
          <span id={titleId}>{title} settings</span>
        </h3>
        <Suspense fallback={<Skeleton aria-label="Loading addon settings" className="mx-4 h-40" />}>
          <AddonSettings key={workspace.id} name={name} workspace={workspace} canEdit={canEdit} labelledBy={titleId} onDirtyChange={onDirtyChange} onClose={close} />
        </Suspense>
      </div>
      {asking && (
        <Dialog open onOpenChange={(o) => !o && keep()}>
          <DialogContent className="max-w-md border-border bg-surface">
            <DialogHeader>
              <DialogTitle>Discard unsaved changes?</DialogTitle>
              <DialogDescription>The {title} settings have changes that are not saved.</DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="ghost" onClick={keep}>
                Keep editing
              </Button>
              <Button variant="destructive" onClick={() => answer(asking.discard)}>
                Discard changes
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </>
  )
}
