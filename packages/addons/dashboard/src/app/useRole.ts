import { roleOf } from '@/api/permissions'
import type { Role } from '@/api/types'
import { useWorkspace } from './workspace'
import { useMe } from '@/app/useMe'

/** The viewer's role in `workspaceId` (default: the current workspace); undefined while loading or when not a member. */
export function useRole(workspaceId?: string): Role | undefined {
  const { workspace, workspaces } = useWorkspace()
  const me = useMe(workspaceId)
  const ws = workspaceId ? workspaces.find((w) => w.id === workspaceId) : workspace
  return roleOf(ws, me.data?.person)
}
