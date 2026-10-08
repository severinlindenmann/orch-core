import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { Workspace } from '@/api/types'

interface WorkspaceCtx {
  workspace: Workspace | undefined
  workspaces: Workspace[]
  /** Switches the current (mock) workspace; every workspace-keyed query refetches. */
  setWorkspaceId: (id: string) => void
}

const Ctx = createContext<WorkspaceCtx>({ workspace: undefined, workspaces: [], setWorkspaceId: () => {} })

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient()
  const { data = [] } = useQuery({ queryKey: ['workspaces'], queryFn: api.getWorkspaces })
  const [id, setId] = useState<string | null>(null)
  const setWorkspaceId = useCallback(
    (next: string) => {
      setId(next)
      void qc.invalidateQueries()
    },
    [qc],
  )
  const value = useMemo<WorkspaceCtx>(
    () => ({ workspaces: data, workspace: data.find((w) => w.id === id) ?? data[0], setWorkspaceId }),
    [data, id, setWorkspaceId],
  )
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export const useWorkspace = () => useContext(Ctx)
