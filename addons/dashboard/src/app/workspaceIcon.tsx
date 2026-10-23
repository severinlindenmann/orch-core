// A workspace's icon: one of a small built-in set plus one of a few colours (no uploads). The default comes from the
// workspace name, so it is stable without anyone choosing. A choice is a dashboard-local preference (localStorage, per
// browser and workspace id): it is not signed, not a ticket field and not part of the core protocol.

import { useCallback, useSyncExternalStore } from 'react'
import { Anchor, Briefcase, Building2, Cloud, Code2, Compass, Flame, Folder, Globe, Heart, Leaf, Rocket, Star, Zap, type LucideIcon } from 'lucide-react'
import { cn } from '@/lib/utils'

export const WORKSPACE_ICONS: { id: string; label: string; Icon: LucideIcon }[] = [
  { id: 'folder', label: 'Folder', Icon: Folder },
  { id: 'rocket', label: 'Rocket', Icon: Rocket },
  { id: 'briefcase', label: 'Briefcase', Icon: Briefcase },
  { id: 'building', label: 'Building', Icon: Building2 },
  { id: 'code', label: 'Code', Icon: Code2 },
  { id: 'cloud', label: 'Cloud', Icon: Cloud },
  { id: 'compass', label: 'Compass', Icon: Compass },
  { id: 'globe', label: 'Globe', Icon: Globe },
  { id: 'leaf', label: 'Leaf', Icon: Leaf },
  { id: 'flame', label: 'Flame', Icon: Flame },
  { id: 'zap', label: 'Bolt', Icon: Zap },
  { id: 'anchor', label: 'Anchor', Icon: Anchor },
  { id: 'heart', label: 'Heart', Icon: Heart },
  { id: 'star', label: 'Star', Icon: Star },
]

/** Mid-tone colours that read on both the light and the dark theme (the tile is a tint of the colour). */
export const WORKSPACE_COLOURS: { id: string; label: string; value: string }[] = [
  { id: 'orange', label: 'Orange', value: '#ea7a2f' },
  { id: 'blue', label: 'Blue', value: '#3b82f6' },
  { id: 'green', label: 'Green', value: '#22a06b' },
  { id: 'purple', label: 'Purple', value: '#8b5cf6' },
  { id: 'pink', label: 'Pink', value: '#e0457b' },
  { id: 'teal', label: 'Teal', value: '#14a3b8' },
  { id: 'amber', label: 'Amber', value: '#c9971c' },
  { id: 'slate', label: 'Grey', value: '#64748b' },
]

export interface WorkspaceIconChoice {
  icon: string
  colour: string
}

/** FNV-1a: small, stable, the same in every browser. */
function hash(s: string): number {
  let h = 0x811c9dc5
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 0x01000193) >>> 0
  }
  return h
}

/** The icon and colour a workspace gets when nobody chose: derived from its name alone. */
export function defaultWorkspaceIcon(name: string): WorkspaceIconChoice {
  const h = hash(name.trim().toLowerCase())
  return { icon: WORKSPACE_ICONS[h % WORKSPACE_ICONS.length].id, colour: WORKSPACE_COLOURS[(h >>> 8) % WORKSPACE_COLOURS.length].id }
}

const keyOf = (id: string) => `orch.wsicon.${id}`
const listeners = new Set<() => void>()
const memory = new Map<string, string>() // used when storage is unavailable: the choice then lasts for this page only

function readRaw(id: string): string | null {
  try {
    return localStorage.getItem(keyOf(id))
  } catch {
    return memory.get(id) ?? null
  }
}

/** A stored choice, or null when none or when it is not one of the built-in icons and colours (unknown values are ignored). */
function parse(raw: string | null): Partial<WorkspaceIconChoice> | null {
  if (!raw) return null
  try {
    const v = JSON.parse(raw) as Partial<WorkspaceIconChoice>
    return {
      icon: WORKSPACE_ICONS.some((i) => i.id === v.icon) ? v.icon : undefined,
      colour: WORKSPACE_COLOURS.some((c) => c.id === v.colour) ? v.colour : undefined,
    }
  } catch {
    return null
  }
}

/** The workspace's icon: its own choice where there is one (each half separately), the name's default otherwise. */
export function workspaceIconOf(id: string, name: string): WorkspaceIconChoice {
  const d = defaultWorkspaceIcon(name)
  const own = parse(readRaw(id))
  return { icon: own?.icon ?? d.icon, colour: own?.colour ?? d.colour }
}

export function setWorkspaceIcon(id: string, choice: WorkspaceIconChoice | null) {
  const raw = choice ? JSON.stringify(choice) : null
  try {
    if (raw) localStorage.setItem(keyOf(id), raw)
    else localStorage.removeItem(keyOf(id))
  } catch {
    if (raw) memory.set(id, raw)
    else memory.delete(id)
  }
  listeners.forEach((l) => l())
}

const subscribe = (cb: () => void) => {
  listeners.add(cb)
  window.addEventListener('storage', cb)
  return () => {
    listeners.delete(cb)
    window.removeEventListener('storage', cb)
  }
}

/** The icon choice of a workspace, live: changes in Settings show in the menu at once (and across tabs). */
export function useWorkspaceIcon(id: string | undefined, name: string | undefined): WorkspaceIconChoice & { set: (c: WorkspaceIconChoice | null) => void; custom: boolean } {
  const raw = useSyncExternalStore(subscribe, () => (id ? readRaw(id) : null))
  const set = useCallback((c: WorkspaceIconChoice | null) => id && setWorkspaceIcon(id, c), [id])
  const d = defaultWorkspaceIcon(name ?? '')
  const own = parse(raw)
  return { icon: own?.icon ?? d.icon, colour: own?.colour ?? d.colour, set, custom: !!own }
}

/** The tile: the icon on a tint of its colour. Decorative; the name beside it (or the button's label) says which workspace. */
export function WorkspaceIcon({ icon, colour, className, size = 'md' }: WorkspaceIconChoice & { className?: string; size?: 'sm' | 'md' }) {
  const Icon = (WORKSPACE_ICONS.find((i) => i.id === icon) ?? WORKSPACE_ICONS[0]).Icon
  const value = (WORKSPACE_COLOURS.find((c) => c.id === colour) ?? WORKSPACE_COLOURS[0]).value
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

/** A workspace's tile that follows its stored choice (used in the switcher rows). */
export function WorkspaceIconFor({ id, name, ...rest }: { id: string; name: string; className?: string; size?: 'sm' | 'md' }) {
  const { icon, colour } = useWorkspaceIcon(id, name)
  return <WorkspaceIcon icon={icon} colour={colour} {...rest} />
}
