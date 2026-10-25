import { useBlocker } from '@tanstack/react-router'
import { Dialog as SheetPrimitive } from 'radix-ui'
import { XIcon } from 'lucide-react'
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
/** Each request is its own: an older request's lapse never clears a newer one for the same addon. */
let focusRequest = 0
export const requestSettingsFocus = (name: string | null) => {
  focusSettingsOf = name
  const mine = ++focusRequest
  // A request that no row takes soon (the list was not drawn again) lapses, so it cannot steal focus on a later visit.
  if (name) setTimeout(() => focusRequest === mine && (focusSettingsOf = null), 2000)
}
export function takeSettingsFocus(name: string): boolean {
  if (focusSettingsOf !== name) return false
  focusSettingsOf = null
  return true
}

/**
 * One addon's settings form as a side panel over the list (a full-height sheet on a phone with a sticky footer). It is
 * open for one addon at a time: on Settings > Add-ons the route /settings/addon/<name> names it, on the Add-ons page the
 * page does. The list stays usable behind it (not modal), so another row's Settings button works. Unsaved changes are never lost silently: every way
 * out (Cancel, Esc, another row's Settings, Back, the palette, a link, a workspace switch) asks first. Focus goes
 * back to the row's Settings button; a deep link scrolls the panel into view.
 */
export function AddonSettingsPanel({ name, workspace, canEdit, onClose }: { name: string; workspace: Workspace; canEdit: boolean; /** Leaves the panel (a route change or the page's state). Called only once nothing unsaved is at stake. */ onClose: () => void | Promise<void> }) {
  const addons = useAddons()
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
  // Every way out asks first when something is unsaved; Cancel, Esc and the X all come here.
  const close = (): void | Promise<void> => {
    let leaving: void | Promise<void> = undefined
    guard(() => {
      requestSettingsFocus(name) // the row's Settings button takes focus once the list is back (not when the question holds it)
      leaving = onClose()
    })
    return leaving
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
      <SheetPrimitive.Root modal={false} open onOpenChange={(o) => !o && void close()}>
        <SheetPrimitive.Portal>
          <SheetPrimitive.Content
            data-slot="addon-settings-sheet"
            aria-labelledby={titleId}
            aria-describedby={undefined}
            // The list behind stays usable: a click on it is not a dismissal (leaving asks through the route blocker).
            onInteractOutside={(e) => e.preventDefault()}
            onOpenAutoFocus={(e) => {
              e.preventDefault()
              panel.current?.querySelector<HTMLElement>('input, select, textarea, button')?.focus()
            }}
            onCloseAutoFocus={(e) => e.preventDefault()}
            className="fixed inset-y-0 right-0 z-40 flex h-full w-full flex-col border-l border-border bg-bg shadow-lg data-[state=closed]:animate-out data-[state=open]:animate-in data-[state=closed]:slide-out-to-right data-[state=open]:slide-in-from-right sm:max-w-[500px]"
          >
            <div ref={panel} role="group" id={`addon-settings-panel-${name}`} aria-labelledby={titleId} className="flex min-h-0 flex-1 flex-col pt-4">
              <div className="mb-3 flex items-center gap-2 px-4 pr-12">
                <AddonBadge name={name} />
                <SheetPrimitive.Title id={titleId} className="text-sm font-semibold">
                  {title} settings
                </SheetPrimitive.Title>
              </div>
              <Suspense fallback={<Skeleton aria-label="Loading addon settings" className="mx-4 h-40" />}>
                <AddonSettings key={workspace.id} name={name} workspace={workspace} canEdit={canEdit} labelledBy={titleId} onDirtyChange={onDirtyChange} onClose={close} />
              </Suspense>
            </div>
            <SheetPrimitive.Close className="absolute right-4 top-4 rounded-xs opacity-70 hover:opacity-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring">
              <XIcon className="size-4" />
              <span className="sr-only">Close</span>
            </SheetPrimitive.Close>
          </SheetPrimitive.Content>
        </SheetPrimitive.Portal>
      </SheetPrimitive.Root>
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
