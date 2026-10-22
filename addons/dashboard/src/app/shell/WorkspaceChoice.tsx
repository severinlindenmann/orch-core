import { Layers, ServerOff } from 'lucide-react'
import type { Workspace } from '@/api/types'
import { uuid8 } from '@/api/workspaces'
import { visible } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { useWorkspace } from '../workspace'
import { usePageHeader } from './ShellUi'

/**
 * A `/w/<PREFIX>` address that two or more of the viewer's workspaces share, without `?ws=` to tell them apart (design 5):
 * the page asks which one. Nothing of either workspace is shown until the person picks.
 */
export function WorkspaceChoice({ prefix, choices }: { prefix: string; choices: Workspace[] }) {
  usePageHeader('Which workspace?')
  const { setWorkspaceId } = useWorkspace()
  return (
    <div className="mx-auto mt-16 max-w-md rounded-lg border border-border bg-surface p-8" role="status">
      <Layers className="mx-auto size-7 text-text-muted" aria-hidden />
      <h1 className="mt-3 text-center text-lg font-semibold">Which workspace?</h1>
      <p className="mt-1.5 text-center text-[13px] text-text-muted">
        More than one workspace of yours is called <span className="font-mono">{prefix}</span>. This link does not say which.
      </p>
      <ul className="mt-4 space-y-2">
        {choices.map((w) => (
          <li key={w.id}>
            <Button variant="outline" className="h-auto w-full justify-between px-3 py-2 text-left" onClick={() => setWorkspaceId(w.id, { url: 'replace' })}>
              <span className="min-w-0">
                <span className="block font-mono text-[11px] text-text-faint">{w.id}</span>
                <span className="block truncate text-[13px]">
                  <bdi>{visible(w.name)}</bdi>
                </span>
              </span>
              <span className="rounded bg-surface-3 px-1 font-mono text-[10px] text-text-muted">{uuid8(w.id)}</span>
            </Button>
          </li>
        ))}
      </ul>
    </div>
  )
}

/** An address that names (by `?ws=`) a workspace whose host is not running. The others keep working. */
export function WorkspaceStopped({ workspace }: { workspace: Workspace }) {
  const unsafe = workspace.host_state === 'workspace.name_invalid'
  usePageHeader(unsafe ? 'Workspace not shown' : workspace.host_state === 'addon.disabled' ? 'Dashboard setup' : workspace.host_message ? 'Workspace can\'t be opened' : 'Host not running')
  if (unsafe)
    return (
      <div className="mx-auto mt-16 max-w-md rounded-lg border border-border bg-surface p-8 text-center" role="status">
        <ServerOff className="mx-auto size-7 text-text-muted" aria-hidden />
        <h1 className="mt-3 text-lg font-semibold">This workspace&apos;s name can&apos;t be shown safely</h1>
        <p className="mt-1.5 text-[13px] text-text-muted">
          Workspace <span className="font-mono">{uuid8(workspace.id)}</span> has a name with hidden characters, so it is not shown. Your other workspaces keep working.
        </p>
      </div>
    )
  if (workspace.host_state === 'addon.disabled')
    return (
      <div className="mx-auto mt-16 max-w-md rounded-lg border border-border bg-surface p-8 text-center" role="status">
        <ServerOff className="mx-auto size-7 text-text-muted" aria-hidden />
        <h1 className="mt-3 text-lg font-semibold">The dashboard is not switched on yet</h1>
        <p className="mt-1.5 text-[13px] text-text-muted">
          Switching it on needs your approval on the computer where orch runs. Open a Terminal window there, run this
          command and, when asked, enter your orch passphrase:
        </p>
        <p className="mt-3 overflow-x-auto rounded bg-surface-3 px-3 py-2 font-mono text-xs whitespace-nowrap select-all">
          orch addon grant dashboard
        </p>
        <p className="mt-3 text-[13px] text-text-muted">Then reload this page.</p>
        <Button variant="outline" size="sm" className="mt-3" onClick={() => window.location.reload()}>
          Reload
        </Button>
      </div>
    )
  if (workspace.host_message)
    return (
      <div className="mx-auto mt-16 max-w-md rounded-lg border border-border bg-surface p-8 text-center" role="status">
        <ServerOff className="mx-auto size-7 text-text-muted" aria-hidden />
        <h1 className="mt-3 text-lg font-semibold">This workspace can&apos;t be opened</h1>
        <p className="mt-1.5 text-[13px] text-text-muted">
          The host says: <bdi>{visible(workspace.host_message)}</bdi>
        </p>
        <p className="mt-1.5 text-[13px] text-text-muted">Your other workspaces keep working.</p>
      </div>
    )
  return (
    <div className="mx-auto mt-16 max-w-md rounded-lg border border-border bg-surface p-8 text-center" role="status">
      <ServerOff className="mx-auto size-7 text-text-muted" aria-hidden />
      <h1 className="mt-3 text-lg font-semibold">Host not running</h1>
      <p className="mt-1.5 text-[13px] text-text-muted">
        The host of workspace <span className="font-mono">{uuid8(workspace.id)}</span> does not answer. Start it and reload this page. Your other workspaces keep working.
      </p>
    </div>
  )
}
