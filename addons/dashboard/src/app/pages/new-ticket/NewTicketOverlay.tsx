import { useQuery } from '@tanstack/react-query'
import { useBlocker, useNavigate } from '@tanstack/react-router'
import { Maximize2 } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '@/api/client'
import { can, roleOf } from '@/api/permissions'
import type { TicketDocument } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Skeleton } from '@/components/ui/skeleton'
import { useSwitchGuard, useWorkspace } from '@/app/workspace'
import { EMPTY, NewTicketForm, carryDraftToPage, draftKey, writeDraft } from './index'
import { QuickTicket } from './QuickTicket'
import { guessType, quickTitle } from './quickRules'
import { useCreatedToast } from './useQuickCreate'

/**
 * New ticket as an overlay: a large right-hand sheet over the current page with a Quick ticket line on top and the
 * same form as /tickets/new below ("More sections" folded). Nothing typed is lost silently: every way out (Cancel, Esc,
 * overlay, X, Back, a link, the palette, a workspace switch) asks "Discard unsaved changes?". "Open full page" carries
 * the draft to /tickets/new. The focus goes back to what opened it.
 */
export function NewTicketOverlay({ onClose, opener }: { onClose: () => void; opener: HTMLElement | null }) {
  const { workspace } = useWorkspace()
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: api.getMe })
  const navigate = useNavigate()
  const createdToast = useCreatedToast()
  const [quick, setQuick] = useState('')
  const quickRef = useRef<HTMLInputElement>(null)
  const formDirty = useRef(false)
  const leaving = useRef(false)
  const restoreFocus = useRef(true)
  const dirty = () => !leaving.current && (formDirty.current || !!quick.trim())
  const [asking, setAsking] = useState<{ keep: () => void; discard: () => void } | null>(null)
  const onDirtyChange = useCallback((d: boolean) => void (formDirty.current = d), [])

  const discardDraft = () => {
    if (me && workspace) writeDraft(draftKey(me.person, workspace.id), null)
  }
  // The workspace guard is registered once; it reads the current state through these.
  const dirtyRef = useRef(dirty)
  dirtyRef.current = dirty
  const discardDraftRef = useRef(discardDraft)
  discardDraftRef.current = discardDraft

  // Route changes while something is typed (Back, the palette, a toast's Open).
  const blocker = useBlocker({ shouldBlockFn: dirty, withResolver: true, enableBeforeUnload: false })
  useEffect(() => {
    if (blocker.status === 'blocked')
      setAsking({
        keep: blocker.reset,
        discard: () => {
          discardDraft()
          leaving.current = true
          blocker.proceed()
        },
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [blocker.status])
  // A workspace switch happens by state, not by route: ask first as well.
  const guard = useCallback(
    (proceed: () => void) => {
      if (!dirtyRef.current()) return proceed()
      setAsking({
        keep: () => {},
        discard: () => {
          discardDraftRef.current()
          leaving.current = true
          proceed()
          onClose()
        },
      })
    },
    [onClose],
  )
  useSwitchGuard(guard)

  const requestClose = () => {
    if (!dirty()) return onClose()
    setAsking({
      keep: () => {},
      discard: () => {
        discardDraft()
        leaving.current = true
        onClose()
      },
    })
  }
  const answer = (f: () => void) => {
    setAsking(null)
    f()
  }

  const created = (ticket: TicketDocument) => {
    createdToast(ticket)
    leaving.current = true
    onClose()
  }
  const quickCreated = (ticket: TicketDocument) => {
    // A half-written form below stays open; the quick line is ready for the next one.
    if (!formDirty.current) return created(ticket)
    createdToast(ticket)
    quickRef.current?.focus()
  }

  const openFullPage = () => {
    // The form autosaves its draft; a quick line typed into an empty form goes along as that draft.
    if (me && workspace && !formDirty.current && quick.trim()) writeDraft(draftKey(me.person, workspace.id), { ...EMPTY, type: guessType(quick), title: quickTitle(quick), sections: { requirements: quick.trim() } })
    leaving.current = true
    restoreFocus.current = false
    carryDraftToPage()
    void navigate({ to: '/tickets/new' })
  }

  const canCreate = !!me && !!workspace && can(roleOf(workspace, me.person), 'ticket.create')

  return (
    <>
      <Sheet open onOpenChange={(o) => !o && requestClose()}>
        <SheetContent
          side="right"
          className="w-[min(92vw,56rem)] gap-0 border-border bg-surface p-0 sm:max-w-none"
          onOpenAutoFocus={(e) => {
            e.preventDefault()
            quickRef.current?.focus()
          }}
          onCloseAutoFocus={(e) => {
            e.preventDefault()
            if (!restoreFocus.current) return
            ;(opener?.isConnected ? opener : document.getElementById('main'))?.focus()
          }}
        >
          <SheetHeader className="border-b border-border pr-12">
            <SheetTitle className="text-base">New ticket</SheetTitle>
            <SheetDescription className="sr-only">Create a ticket in one line, or fill in the form.</SheetDescription>
          </SheetHeader>
          <div className="min-h-0 flex-1 overflow-y-auto px-6 pt-4">
            {!me || !workspace ? (
              <Skeleton className="h-96" aria-label="Loading" />
            ) : (
              <>
                <QuickTicket workspaceId={workspace.id} canCreate={canCreate} value={quick} onChange={setQuick} onCreated={quickCreated} inputRef={quickRef} />
                <div className="my-4 flex items-center gap-3 text-[12px] text-text-faint" aria-hidden>
                  <span className="h-px flex-1 bg-border" />
                  or fill in the form
                  <span className="h-px flex-1 bg-border" />
                </div>
                <NewTicketForm
                  key={`${workspace.id}:${me.person}`}
                  me={me}
                  workspace={workspace}
                  variant="overlay"
                  onDirtyChange={onDirtyChange}
                  onCreated={created}
                  onCancel={requestClose}
                  footerExtra={
                    <Button type="button" variant="ghost" onClick={openFullPage} className="ml-auto">
                      <Maximize2 />
                      Open full page
                    </Button>
                  }
                />
              </>
            )}
          </div>
        </SheetContent>
      </Sheet>
      {asking && (
        <Dialog open onOpenChange={(o) => !o && answer(asking.keep)}>
          <DialogContent className="max-w-md border-border bg-surface">
            <DialogHeader>
              <DialogTitle>Discard unsaved changes?</DialogTitle>
              <DialogDescription>What you typed has not been saved as a ticket.</DialogDescription>
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
