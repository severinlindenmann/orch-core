import { useQuery } from '@tanstack/react-query'
import { queries } from '@/api/queries'
import { useWorkspace } from './workspace'

/** The viewer in `workspaceId` (default: the workspace the page is showing): their person id, name and role there. */
export function useMe(workspaceId?: string) {
  const { workspace } = useWorkspace()
  return useQuery(queries.me(workspaceId ?? workspace?.id))
}
