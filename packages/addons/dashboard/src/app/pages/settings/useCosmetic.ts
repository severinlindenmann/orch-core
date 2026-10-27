import { useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { ApiError, type Cosmetic, type Workspace } from '@/api/types'
import { toastApiError } from '@/app/toast'

/** What one save changes: the name, the icon (null: back to the automatic one), or both. */
export type CosmeticChange = { name?: string; icon?: Cosmetic['icon'] }

const isConflict = (e: unknown) => e instanceof ApiError && e.code === 'cosmetic.conflict'

/**
 * D6: the workspace name and icon, unsigned and owner only, saved with one click (no hand-off). Every save sends what the
 * page showed (`if_current`); the host refuses a stale one (`cosmetic.conflict`). "Saved" offers Undo for ten seconds,
 * which puts the old values back against the values the save returned.
 */
export function useCosmetic(workspace: Pick<Workspace, 'id' | 'name' | 'icon'>) {
  const qc = useQueryClient()
  const ws = workspace.id
  // The name and icon live in the workspace list (switcher, sidebar, palette) and the identity card.
  const refresh = () => Promise.all([qc.invalidateQueries({ queryKey: ['workspaces'] }), qc.invalidateQueries({ queryKey: ['identity', ws] })])
  const conflict = async () => {
    toast('Someone changed it meanwhile')
    await refresh()
  }

  const undo = async (old: CosmeticChange, now: Cosmetic) => {
    try {
      await api.setCosmetic(ws, { ...old, if_current: now })
      await refresh()
    } catch (e) {
      if (isConflict(e)) await conflict()
      else toastApiError(e, 'Could not undo')
    }
  }

  const save = async (change: CosmeticChange): Promise<boolean> => {
    const before: Cosmetic = { name: workspace.name, icon: workspace.icon ?? null }
    const old: CosmeticChange = {
      ...(change.name !== undefined ? { name: before.name } : {}),
      ...(change.icon !== undefined ? { icon: before.icon } : {}),
    }
    try {
      const now = await api.setCosmetic(ws, { ...change, if_current: before })
      await refresh()
      toast('Saved', { action: { label: 'Undo', onClick: () => void undo(old, now) }, duration: 10000 })
      return true
    } catch (e) {
      if (isConflict(e)) await conflict()
      else toastApiError(e, 'Could not save')
      return false
    }
  }

  return { save }
}
