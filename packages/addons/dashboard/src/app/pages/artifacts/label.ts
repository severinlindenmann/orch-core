import type { ArtifactItem, Member } from '@/api/types'
import { agentName, displayName } from '../ticket/shared'

/** "Claude Code for Severin", "Mara", "publish addon", "orch". */
export function byLabel(by: ArtifactItem['by'], members: Member[]): string {
  if (by.kind === 'agent') return `${agentName(by.id)}${by.for ? ` for ${displayName(members, by.for)}` : ''}`
  if (by.kind === 'person') return displayName(members, by.id)
  if (by.kind === 'addon') return `${by.id} addon`
  return 'orch'
}
