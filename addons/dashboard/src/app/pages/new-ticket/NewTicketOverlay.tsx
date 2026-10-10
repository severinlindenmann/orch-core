import { useQuery } from '@tanstack/react-query'
import { useBlocker, useNavigate } from '@tanstack/react-router'
import { Maximize2 } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { can, roleOf } from '@/api/permissions'
import type { TicketDocument } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Skeleton } from '@/components/ui/skeleton'
import { useSwitchGuard, useWorkspace } from '@/app/workspace'
import { EMPTY, NewTicketForm, carryDraftToPage, draftKey, readDraft, writeDraft } from './index'
import { QuickTicket } from './QuickTicket'
import { guessType, quickTitle } from './quickRules'
import { useCreatedToast } from './useQuickCreate'
import { queries } from '@/api/queries'

/**
 * New ticket as an overlay: a large right-hand sheet over the current page with a Quick ticket line on top and the
 * same form as /tickets/new below ("More sections" folded). Nothing typed is lost silently: every way out (Cancel, Esc,
 * overlay, X, Back, a link, the palette, a workspace switch) asks "Discard unsaved changes?". "Open full page" carries
 * the draft to /tickets/new. The focus goes back to what opened it.
 */
export function NewTicketOverlay({ onClose, opener }: { onClose: () => void; opener: HTMLElement | null }) {
  const { workspace } = useWorkspace()
  const { data: me } = useQuery(queries.me())
  const navigate = useNavigate()
  const createdToast = useCreatedToast()
  const [quick, setQuick] = useState('')
  const quickRef = useRef<HTMLInputElement>(null)
  const formDirty = useRef(false)
  const leaving = useRef(false)
  const restoreFocus = useRef(true)
  const dirty = () => !leaving.current && (formDirty.current || !!quick.trim())
  /** The prompt's answers; `keepDraft` (close and keep the autosaved form draft) only when the form has text. */
  const [asking, setAsking] = useState<{ keep: () => void; discard: () => void; keepDraft?: () => void } | null>(null)
  const onDirtyChange = useCallback((d: boolean) => void (formDirty.current = d), [])

  const discardDraft = () => {
    if (me && workspace) writeDraft(draftKey(me.person, workspace.id), null)
  }
  /** Puts the quick line into the stored draft so it is never dropped: appended to the requirements, or a new draft. */
  const foldQuickIntoDraft = () => {
    const text = quick.trim()
    if (!me || !workspace || !text) return
    const key = draftKey(me.person, workspace.id)
    const stored = formDirty.current ? readDraft(key) : null
    if (stored) {
      const req = stored.sections.requirements?.trim()
      writeDraft(key, { ...stored, sections: { ...stored.sections, requirements: req ? `${req}\n\n${text}` : text } })
    } else writeDraft(key, { ...EMPTY, type: guessType(text), title: quickTitle(text), sections: { requirements: text } })
  }
  /** Close and keep the draft (the form autosaves it; the quick line goes into it). */
  const keepDraftThen = (go: () => void) => () => {
    foldQuickIntoDraft()
    leaving.current = true
    go()
  }
  // The workspace guard is registered once; it reads the current state through these.
  const dirtyRef = useRef(dirty)
  dirtyRef.current = dirty
  const discardDraftRef = useRef(discardDraft)
  discardDraftRef.current = discardDraft
  const keepDraftThenRef = useRef(keepDraftThen)
  keepDraftThenRef.current = keepDraftThen

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
        keepDraft: formDirty.current ? keepDraftThen(blocker.proceed) : undefined,
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
        keepDraft: formDirty.current
          ? keepDraftThenRef.current(() => {
              proceed()
              onClose()
            })
          : undefined,
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
      keepDraft: formDirty.current ? keepDraftThen(onClose) : undefined,
    })
  }
  const answer = (f: () => void) => {
    setAsking(null)
    f()
  }

  /** The full form's Create: say so (Open, Undo) and close. */
  const created = (ticket: TicketDocument) => {
    if (workspace) createdToast(ticket, workspace.id)
    leaving.current = true
    onClose()
  }
  /** A quick ticket (useQuickCreate has already said so): close, unless a half-written form below stays open. */
  const quickCreated = () => {
    if (formDirty.current) return quickRef.current?.focus()
    leaving.current = true
    onClose()
  }

  const openFullPage = () => {
    // The form autosaves its draft; the quick line goes along (appended to the requirements, or as the draft).
    foldQuickIntoDraft()
    leaving.current = true
    restoreFocus.current = false
    carryDraftToPage()
    void navigate({ to: '/tickets/new' })
  }

  const canCreate = !!me && !!workspace && can(roleOf(workspace, me.person), 'ticket.create')

  return (
    <>
      <Sheet open onOpenChange={(o) => !o && requestClose()}>
        {/* Over a page with the terminal docked on the right it stops at the dock and sizes to the page area (N11). */}
        <SheetContent
          side="right"
          className="right-[var(--dock-right,0px)] w-[min(56rem,calc(100vw-var(--dock-right,0px)-4rem))] gap-0 border-border bg-surface p-0 sm:max-w-none"
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
          <SheetHeader className="border-b border-border pr-20">
            <SheetTitle className="text-base">New ticket</SheetTitle>
            <Button type="button" variant="ghost" size="icon" className="absolute right-10 top-2.5 size-7 text-text-muted" aria-label="Open full page" title="Open full page" onClick={openFullPage}>
              <Maximize2 className="size-4" />
            </Button>
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
              <DialogDescription>
                What you typed has not been saved as a ticket.{asking.keepDraft && ' You can close and keep it as a draft for next time.'}
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="ghost" onClick={() => answer(asking.keep)}>
                Keep editing
              </Button>
              {asking.keepDraft && (
                <Button variant="outline" onClick={() => answer(asking.keepDraft!)}>
                  Close, keep draft
                </Button>
              )}
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
