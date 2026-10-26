// Wiki on a real host (add-on): the pages of the project's wiki folder, read only. When the project has no wiki folder,
// an owner or maintainer gets one button, "Create the wiki folder" (it adds wiki/readme.md and commits only that file).
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { hasFeature } from '@/api/features'
import { atLeast } from '@/api/permissions'
import { createWikiFolder, wikiQueries } from '@/api/wiki'
import { LoadFailed } from '@/components/LoadFailed'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { usePageHeader } from '@/app/shell/ShellUi'
import { useRole } from '@/app/useRole'
import { useWorkspace } from '@/app/workspace'

function Page({ ws, slug, onBack }: { ws: string; slug: string; onBack: () => void }) {
  const page = useQuery(wikiQueries.page(ws, slug))
  return (
    <div className="space-y-3">
      <Button size="sm" variant="ghost" onClick={onBack}>All pages</Button>
      {page.isError ? (
        <LoadFailed what="this page" onRetry={() => void page.refetch()} />
      ) : page.data ? (
        <>
          <h2 className="text-lg font-semibold tracking-tight"><Inline>{page.data.title}</Inline></h2>
          {page.data.markdown === null ? (
            <p className="text-[13px] text-text-muted">This page is too large to show here.</p>
          ) : (
            <pre className="whitespace-pre-wrap break-words rounded-lg border border-border bg-surface p-3 text-[13px]"><Inline>{page.data.markdown}</Inline></pre>
          )}
        </>
      ) : (
        <p role="status" className="text-[13px] text-text-muted">Reading the page…</p>
      )}
    </div>
  )
}

export function WikiPage() {
  usePageHeader('Wiki')
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const role = useRole()
  const qc = useQueryClient()
  const [open, setOpen] = useState<string | null>(null)
  const served = !!ws && hasFeature('wiki', ws)
  const list = useQuery({ ...wikiQueries.list(ws ?? ''), enabled: served })
  const create = useMutation({
    mutationFn: () => createWikiFolder(ws as string),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['wiki', ws] }),
  })
  if (!workspace || !ws) return null
  if (!served) {
    return (
      <div className="max-w-2xl space-y-2">
        <h1 className="text-xl font-semibold tracking-tight">Wiki</h1>
        <p data-testid="wiki-unavailable" className="text-[13px] text-text-muted">
          Wiki is off for this workspace. Owners can turn it on in Settings, under Add-ons.
        </p>
      </div>
    )
  }
  if (list.isError) return <LoadFailed what="the wiki" onRetry={() => void list.refetch()} />
  const data = list.data
  const mayCreate = atLeast(role, 'maintainer')
  return (
    <div className="w-full max-w-4xl space-y-4">
      <h1 className="text-xl font-semibold tracking-tight">Wiki</h1>
      {!data ? (
        <p role="status" className="text-[13px] text-text-muted">Reading the wiki…</p>
      ) : data.state === 'no_folder' ? (
        <div data-testid="wiki-empty" className="space-y-3 rounded-lg border border-border bg-surface p-4">
          <p className="text-[13px] text-text-muted">No wiki folder in this project yet.</p>
          {mayCreate ? (
            <Button disabled={create.isPending} onClick={() => create.mutate()}>Create the wiki folder</Button>
          ) : (
            <p className="text-[13px] text-text-faint">Ask an owner or maintainer to create it.</p>
          )}
          {create.isError && <p role="alert" className="text-[13px] text-danger">The wiki folder could not be created. Check that the project is a Git repository, then try again.</p>}
        </div>
      ) : data.state !== 'ok' ? (
        <p data-testid="wiki-state" className="text-[13px] text-text-muted">{data.message}</p>
      ) : open ? (
        <Page ws={ws} slug={open} onBack={() => setOpen(null)} />
      ) : data.pages.length === 0 ? (
        <p data-testid="wiki-none" className="text-[13px] text-text-muted">The wiki folder has no pages yet. Add .md files to it.</p>
      ) : (
        <ul aria-label="Wiki pages" className="divide-y divide-border rounded-lg border border-border bg-surface px-3">
          {data.pages.map((p) => (
            <li key={p.slug} className="flex flex-wrap items-center gap-x-3 py-2.5 text-[13px]">
              <button type="button" className="text-brand hover:underline" onClick={() => setOpen(p.slug)}><Inline>{p.title}</Inline></button>
              <span className="font-mono text-[12px] text-text-faint">{p.slug}</span>
              {p.links > 0 && <span className="ml-auto text-text-muted">{p.links} linked {p.links === 1 ? 'ticket' : 'tickets'}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
