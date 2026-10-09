import { lazy, Suspense, useCallback, useState } from 'react'
import { AddonBadge } from '@/addon-ui/AddonBadge'
import { useAddons } from '@/addon-ui/slots'
import type { Workspace } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Skeleton } from '@/components/ui/skeleton'

// The form (rjsf through AddonNode) loads when the drawer first opens.
const AddonSettings = lazy(() => import('../AddonSettings').then((m) => ({ default: m.AddonSettings })))

/** The id the row's Settings button carries, so closing the drawer can put focus back on it. */
export const settingsButtonAttr = 'data-settings-for'

/**
 * A right-side drawer with one addon's settings form, over the Addons list (route /settings/addon/<name>).
 * Closing with unsaved changes asks first; focus goes back to the row's Settings button.
 */
export function AddonSettingsDrawer({ name, workspace, canEdit, onClose }: { name: string; workspace: Workspace; canEdit: boolean; onClose: () => void }) {
  const addons = useAddons()
  const title = addons.data?.find((a) => a.name === name)?.title ?? name
  const [dirty, setDirty] = useState(false)
  const [asking, setAsking] = useState(false)
  const onDirtyChange = useCallback((d: boolean) => setDirty(d), [])
  const requestClose = () => (dirty ? setAsking(true) : onClose())

  return (
    <>
      <Sheet open onOpenChange={(o) => !o && requestClose()}>
        <SheetContent
          side="right"
          className="w-[min(90vw,40rem)] gap-0 border-border bg-surface p-0 sm:max-w-none"
          onCloseAutoFocus={(e) => {
            e.preventDefault()
            document.querySelectorAll<HTMLElement>(`[${settingsButtonAttr}]`).forEach((b) => b.getAttribute(settingsButtonAttr) === name && b.focus())
          }}
        >
          <SheetHeader className="border-b border-border pr-12">
            <SheetTitle className="flex items-center gap-2 text-base">
              <AddonBadge name={name} />
              {title} settings
            </SheetTitle>
            <SheetDescription className="sr-only">Change how {title} behaves in this workspace.</SheetDescription>
          </SheetHeader>
          <div className="flex min-h-0 flex-1 flex-col pt-4">
            <Suspense fallback={<Skeleton aria-label="Loading addon settings" className="mx-4 h-40" />}>
              <AddonSettings name={name} workspace={workspace} canEdit={canEdit} onDirtyChange={onDirtyChange} onClose={requestClose} />
            </Suspense>
          </div>
        </SheetContent>
      </Sheet>
      {asking && (
        <Dialog open onOpenChange={(o) => !o && setAsking(false)}>
          <DialogContent className="max-w-md border-border bg-surface">
            <DialogHeader>
              <DialogTitle>Discard unsaved changes?</DialogTitle>
              <DialogDescription>The {title} settings have changes that are not saved.</DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="ghost" onClick={() => setAsking(false)}>
                Keep editing
              </Button>
              <Button
                variant="destructive"
                onClick={() => {
                  setAsking(false)
                  onClose()
                }}
              >
                Discard changes
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </>
  )
}
