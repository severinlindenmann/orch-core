// The workspace icon's closed lists, the id-derived default and the checks. Pure data, no React.
//
// The icon is signed with the workspace (`settings.changed` `set.icon`, owner only); core's `orch/wsicons.py` holds the same
// lists and a test pins them equal (also to the JSON schemas). An icon is only ever a pair of ids from these lists: no text,
// no URL, no upload. A value outside the lists is ignored and the default shows.

export const ICON_IDS = ['folder', 'rocket', 'briefcase', 'building', 'code', 'cloud', 'compass', 'globe', 'leaf', 'flame', 'zap', 'anchor', 'heart', 'star'] as const
export const COLOUR_IDS = ['orange', 'blue', 'green', 'purple', 'pink', 'teal', 'amber', 'slate'] as const

export interface WorkspaceIconChoice {
  icon: string
  colour: string
}

export const validIcon = (icon: unknown, colour: unknown): boolean => typeof icon === 'string' && typeof colour === 'string' && (ICON_IDS as readonly string[]).includes(icon) && (COLOUR_IDS as readonly string[]).includes(colour)

/** 32-bit FNV-1a over the UTF-16 code units (core `wsicons.fnv1a` is the same function). */
export function fnv1a(s: string): number {
  let h = 0x811c9dc5
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 0x01000193) >>> 0
  }
  return h
}

/** What a workspace shows when no icon was signed: from its id (signed at creation; never the name, which is not signed and can repeat). */
export function defaultWorkspaceIcon(id: string): WorkspaceIconChoice {
  const h = fnv1a(id.trim().toLowerCase())
  return { icon: ICON_IDS[h % ICON_IDS.length], colour: COLOUR_IDS[(h >>> 8) % COLOUR_IDS.length] }
}

/** The signed icon where it is one of the lists, otherwise the default. */
export function workspaceIconOf(w: { id: string; icon?: { icon?: unknown; colour?: unknown } | null }): WorkspaceIconChoice {
  const own = w.icon
  return own && validIcon(own.icon, own.colour) ? { icon: own.icon as string, colour: own.colour as string } : defaultWorkspaceIcon(w.id)
}

type Named = { id: string; name: string; icon?: { icon?: unknown; colour?: unknown } | null }

/** Another workspace has the same name, or the same symbol (any colour): then the prefix and a short id tail are shown beside the icon. */
export function lookAlike(w: Named, all: Named[]): boolean {
  const mine = workspaceIconOf(w)
  return all.some((o) => {
    if (o.id === w.id) return false
    const theirs = workspaceIconOf(o)
    return o.name.trim().toLowerCase() === w.name.trim().toLowerCase() || theirs.icon === mine.icon
  })
}
