import { createContext, useContext, useMemo, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { Workspace } from '@/api/types'

interface WorkspaceCtx {
  workspace: Workspace | undefined
  workspaces: Workspace[]
  setWorkspaceId: (id: string) => void
}

const Ctx = createContext<WorkspaceCtx>({ workspace: undefined, workspaces: [], setWorkspaceId: () => {} })

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const { data = [] } = useQuery({ queryKey: ['workspaces'], queryFn: api.getWorkspaces })
  const [id, setWorkspaceId] = useState<string | null>(null)
  const value = useMemo<WorkspaceCtx>(
    () => ({ workspaces: data, workspace: data.find((w) => w.id === id) ?? data[0], setWorkspaceId }),
    [data, id],
  )
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export const useWorkspace = () => useContext(Ctx)
