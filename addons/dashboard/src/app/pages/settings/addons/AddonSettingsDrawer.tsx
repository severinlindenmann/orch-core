import { useBlocker, useNavigate } from '@tanstack/react-router'
import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { AddonBadge } from '@/addon-ui/AddonBadge'
import { useAddons } from '@/addon-ui/slots'
import type { Workspace } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Skeleton } from '@/components/ui/skeleton'
import { useSwitchGuard } from '@/app/workspace'

// The form (rjsf through AddonNode) loads when the drawer first opens.
const AddonSettings = lazy(() => import('../AddonSettings').then((m) => ({ default: m.AddonSettings })))

/** The id the row's Settings button carries, so closing the drawer can put focus back on it. */
export const settingsButtonAttr = 'data-settings-for'
/** Where focus goes when the row is not there (after a deep link to an addon without a row). */
export const addonsHeadingAttr = 'data-addons-heading'

/**
 * A right-side drawer with one addon's settings form, over the Addons list (route /settings/addon/<name>).
 * Unsaved changes are never lost silently: every way out (Cancel, Esc, overlay, X, Back, the palette, a link, a
 * workspace switch) asks first. Focus goes back to the row's Settings button.
 */
export function AddonSettingsDrawer({ name, workspace, canEdit }: { name: string; workspace: Workspace; canEdit: boolean }) {
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

  const close = () => void navigate({ to: '/settings/$tab', params: { tab: 'addons' } })
  const answer = (f: () => void) => {
    setAsking(null)
    f()
  }

  return (
    <>
      <Sheet open onOpenChange={(o) => !o && close()}>
        <SheetContent
          side="right"
          className="w-[min(90vw,40rem)] gap-0 border-border bg-surface p-0 sm:max-w-none"
          onCloseAutoFocus={(e) => {
            e.preventDefault()
            const rows = Array.from(document.querySelectorAll<HTMLElement>(`[${settingsButtonAttr}]`)).filter((b) => b.getAttribute(settingsButtonAttr) === name)
            ;(rows[0] ?? document.querySelector<HTMLElement>(`[${addonsHeadingAttr}]`))?.focus()
          }}
        >
          <SheetHeader className="border-b border-border pr-12">
            <SheetTitle className="flex items-center gap-2 text-base">
              <AddonBadge name={name} />
              <span id={titleId}>{title} settings</span>
            </SheetTitle>
            <SheetDescription className="sr-only">Change how {title} behaves in this workspace.</SheetDescription>
          </SheetHeader>
          <div className="flex min-h-0 flex-1 flex-col pt-4">
            <Suspense fallback={<Skeleton aria-label="Loading addon settings" className="mx-4 h-40" />}>
              <AddonSettings key={workspace.id} name={name} workspace={workspace} canEdit={canEdit} labelledBy={titleId} onDirtyChange={onDirtyChange} onClose={close} />
            </Suspense>
          </div>
        </SheetContent>
      </Sheet>
      {asking && (
        <Dialog open onOpenChange={(o) => !o && answer(asking.keep)}>
          <DialogContent className="max-w-md border-border bg-surface">
            <DialogHeader>
              <DialogTitle>Discard unsaved changes?</DialogTitle>
              <DialogDescription>The {title} settings have changes that are not saved.</DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="ghost" onClick={() => answer(asking.keep)}>
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
