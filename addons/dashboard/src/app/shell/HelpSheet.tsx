import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useRouterState } from '@tanstack/react-router'
import { addonActive } from '@/api/addons'
import { api } from '@/api/client'
import { helpPageFor, type HelpRoute } from '@/api/guide'
import { AddonBadge } from '@/addon-ui/AddonBadge'
import { SafeMarkdown } from '@/addon-ui/SafeMarkdown'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Skeleton } from '@/components/ui/skeleton'
import { useWorkspace } from '../workspace'
import { keyboardBusy } from './shortcuts'

interface GuideState {
  help?: { routes: HelpRoute[]; pages: { slug: string; title: string; markdown: string }[] }
}

/**
 * The `?` help sheet (core). It shows the guide addon's page for the current route: the guide sends the route map and
 * the page texts, core picks the page (`helpPageFor`). Without the guide (not installed, off, or not granted) it says so.
 * `?` types a question mark in a field and does nothing while a dialog or menu is open (`keyboardBusy`).
 */
export function HelpSheet() {
  const [open, setOpen] = useState(false)
  const { workspace } = useWorkspace()
  const path = useRouterState({ select: (s) => s.location.pathname })
  const active = !!workspace && addonActive(workspace, 'guide')
  const state = useQuery({
    queryKey: ['addon-state', workspace?.id, 'guide'],
    queryFn: () => api.getAddonState(workspace!.id, 'guide') as Promise<GuideState>,
    enabled: open && active,
    retry: false,
  })

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== '?' || e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented || e.repeat || keyboardBusy(e.target)) return
      e.preventDefault()
      setOpen(true)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const help = state.data?.help
  const slug = help ? helpPageFor(help.routes, path) : undefined
  const page = help?.pages.find((p) => p.slug === slug)
  // Open the guide on the page the sheet showed.
  const openPage = () => {
    setOpen(false)
    if (workspace && slug) void api.runAddonAction(workspace.id, 'guide', 'open', { slug }).catch(() => {})
  }

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetContent side="right" className="w-[480px] max-w-[92vw] gap-0 border-border bg-surface sm:max-w-[480px]">
        <SheetHeader className="border-b border-border">
          <SheetTitle>Help</SheetTitle>
          <SheetDescription className="flex items-center gap-2">
            {active && <AddonBadge name="guide" />}
            Help for this page. Press Esc to close.
          </SheetDescription>
        </SheetHeader>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {!active ? (
            <p className="text-[13px] text-text-muted">The guide is off in this workspace. An owner can turn it on in Settings, under Addons.</p>
          ) : state.isError ? (
            <p className="text-[13px] text-text-muted">The guide could not be loaded.</p>
          ) : page ? (
            <SafeMarkdown text={page.markdown} />
          ) : (
            <Skeleton className="h-40 w-full" />
          )}
        </div>
        {active && (
          <div className="border-t border-border p-4">
            <Link to="/addon/$name/$page" params={{ name: 'guide', page: 'guide' }} onClick={openPage} className="text-[13px] text-brand hover:underline">
              Open the guide
            </Link>
          </div>
        )}
      </SheetContent>
    </Sheet>
  )
}
