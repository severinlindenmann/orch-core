import { Link } from '@tanstack/react-router'
import { SearchX } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useWorkspace } from '../workspace'
import { usePageHeader } from './ShellUi'

/** A `/w/<PREFIX>/…` address for a workspace the viewer does not have (mistyped, or not shared with them). */
export function WorkspaceNotFound({ prefix }: { prefix: string }) {
  usePageHeader('Workspace not found')
  const { workspace } = useWorkspace()
  return (
    <div className="mx-auto mt-16 max-w-md rounded-lg border border-border bg-surface p-8 text-center" role="status">
      <SearchX className="mx-auto size-7 text-text-muted" aria-hidden />
      <h1 className="mt-3 text-lg font-semibold">No workspace {prefix}</h1>
      <p className="mt-1.5 text-[13px] text-text-muted">
        This link names the workspace <span className="font-mono">{prefix}</span>, which is not one of yours on this device. It may be mistyped, or nobody has added you to it.
      </p>
      {workspace && (
        <Button asChild variant="outline" size="sm" className="mt-4">
          <Link to="/">Go to {workspace.name}</Link>
        </Button>
      )}
    </div>
  )
}
