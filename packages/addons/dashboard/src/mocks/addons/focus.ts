import { addonActive } from '@/api/addons'
import type { MockStore } from '../store'
import { registerAddon } from './registry'

registerAddon({ name: 'focus', seed: () => ({}), actions: {} })

export function focusFeatures(store: MockStore, ws: string): string[] {
  return addonActive(store.workspaces.find((workspace) => workspace.id === ws), 'focus') ? ['focus'] : []
}
