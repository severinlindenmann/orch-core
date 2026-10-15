import { roleOf } from '@/api/permissions'
import type { Role, Workspace } from '@/api/types'
import { useMe } from '@/app/useMe'

/** The signed-in person's role in this workspace, from the host's roster (undefined while loading, or if not a member). */
export function useViewerRole(workspace: Workspace): Role | undefined {
  const me = useMe(workspace.id)
  return roleOf(workspace, me.data?.person)
}

export function useViewerPerson(): string | undefined {
  return useMe().data?.person
}
