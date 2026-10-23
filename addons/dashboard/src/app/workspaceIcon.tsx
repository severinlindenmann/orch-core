// A workspace's icon tile: one of a small built-in set plus one of a few colours (no uploads). The icon is the signed
// workspace setting (see api/wsIcons.ts); without one, the default comes from the workspace id. It never replaces the
// prefix and the short id, which stay beside it (they tell same-named workspaces apart).

import { Anchor, Briefcase, Building2, Cloud, Code2, Compass, Flame, Folder, Globe, Heart, Leaf, Rocket, Star, Zap, type LucideIcon } from 'lucide-react'
import { cn } from '@/lib/utils'
import { COLOUR_IDS, ICON_IDS, workspaceIconOf, type WorkspaceIconChoice } from '@/api/wsIcons'
import type { Workspace } from '@/api/types'

const ICON_VIEW: Record<(typeof ICON_IDS)[number], { label: string; Icon: LucideIcon }> = {
  folder: { label: 'Folder', Icon: Folder },
  rocket: { label: 'Rocket', Icon: Rocket },
  briefcase: { label: 'Briefcase', Icon: Briefcase },
  building: { label: 'Building', Icon: Building2 },
  code: { label: 'Code', Icon: Code2 },
  cloud: { label: 'Cloud', Icon: Cloud },
  compass: { label: 'Compass', Icon: Compass },
  globe: { label: 'Globe', Icon: Globe },
  leaf: { label: 'Leaf', Icon: Leaf },
  flame: { label: 'Flame', Icon: Flame },
  zap: { label: 'Bolt', Icon: Zap },
  anchor: { label: 'Anchor', Icon: Anchor },
  heart: { label: 'Heart', Icon: Heart },
  star: { label: 'Star', Icon: Star },
}
export const WORKSPACE_ICONS = ICON_IDS.map((id) => ({ id, ...ICON_VIEW[id] }))

/** Mid-tone colours that read on both the light and the dark theme (the tile is a tint of the colour). */
const COLOUR_VIEW: Record<(typeof COLOUR_IDS)[number], { label: string; value: string }> = {
  orange: { label: 'Orange', value: '#ea7a2f' },
  blue: { label: 'Blue', value: '#3b82f6' },
  green: { label: 'Green', value: '#22a06b' },
  purple: { label: 'Purple', value: '#8b5cf6' },
  pink: { label: 'Pink', value: '#e0457b' },
  teal: { label: 'Teal', value: '#14a3b8' },
  amber: { label: 'Amber', value: '#c9971c' },
  slate: { label: 'Grey', value: '#64748b' },
}
export const WORKSPACE_COLOURS = COLOUR_IDS.map((id) => ({ id, ...COLOUR_VIEW[id] }))

/** The tile: the icon on a tint of its colour. Decorative; the prefix and name beside it say which workspace. */
export function WorkspaceIcon({ icon, colour, className, size = 'md' }: WorkspaceIconChoice & { className?: string; size?: 'sm' | 'md' }) {
  const Icon = ICON_VIEW[icon as keyof typeof ICON_VIEW]?.Icon ?? Folder
  const value = COLOUR_VIEW[colour as keyof typeof COLOUR_VIEW]?.value ?? COLOUR_VIEW.orange.value
  return (
    <span
      aria-hidden
      data-ws-icon={icon}
      data-ws-colour={colour}
      className={cn('inline-flex shrink-0 items-center justify-center rounded-md', size === 'md' ? 'size-7' : 'size-6', className)}
      style={{ color: value, background: `color-mix(in srgb, ${value} 18%, transparent)` }}
    >
      <Icon className={size === 'md' ? 'size-4' : 'size-3.5'} strokeWidth={2} />
    </span>
  )
}

/** A workspace's tile from its signed icon (or the id-derived default). */
export function WorkspaceIconFor({ workspace, ...rest }: { workspace: Pick<Workspace, 'id' | 'icon'>; className?: string; size?: 'sm' | 'md' }) {
  return <WorkspaceIcon {...workspaceIconOf(workspace)} {...rest} />
}
