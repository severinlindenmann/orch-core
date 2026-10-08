import { useQuery } from '@tanstack/react-query'
import { api } from '@/api/client'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { Badge } from '@/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useWorkspace } from '../workspace'

export function Topbar() {
  const { workspace, workspaces, setWorkspaceId } = useWorkspace()
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: api.getMe })

  return (
    <header className="flex h-12 shrink-0 items-center gap-3 border-b border-border bg-bg px-4">
      {workspace && (
        <Select value={workspace.id} onValueChange={setWorkspaceId}>
          <SelectTrigger size="sm" className="w-[220px] bg-surface" aria-label="Workspace">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {workspaces.map((w) => (
              <SelectItem key={w.id} value={w.id}>
                <span className="font-mono text-[11px] text-text-faint">{w.prefix}</span> {w.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
      {workspace?.prefix === 'DEMO' && (
        <Badge variant="outline" className="border-warning/40 text-warning">
          Demo data
        </Badge>
      )}
      <div className="flex-1" />
      {me?.grant && (
        <span className="font-mono text-[11px] text-text-faint">
          grant {me.grant.id} until {me.grant.until.slice(11, 16)}
        </span>
      )}
      {me && (
        <Avatar className="size-7">
          <AvatarFallback className="bg-surface-3 text-[11px]">{me.name.slice(0, 2).toUpperCase()}</AvatarFallback>
        </Avatar>
      )}
    </header>
  )
}
