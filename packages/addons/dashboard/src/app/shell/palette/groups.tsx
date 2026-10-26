import type { ReactNode } from 'react'
import { AddonBadge } from '@/addon-ui/AddonBadge'
import { CommandGroup, CommandItem, CommandShortcut } from '@/components/ui/command'

/** One palette row: icon, label, optional addon badge, and its keyboard shortcut. */
export interface Entry {
  id: string
  label: ReactNode
  icon?: ReactNode
  /** Extra words cmdk-style matching also looks at. */
  hint?: string
  keys?: string
  addon?: string
  disabled?: boolean
  run: () => void
}

/** The heading becomes the group's accessible name, so the groups can be found by role. */
export function Group({ heading, entries }: { heading: string; entries: Entry[] }) {
  if (!entries.length) return null
  return (
    <CommandGroup heading={heading}>
      {entries.map((e) => (
        <CommandItem key={e.id} value={`${heading}-${e.id}`} disabled={e.disabled} onSelect={e.run}>
          {e.icon}
          {e.label}
          {e.addon && <AddonBadge name={e.addon} className="ml-auto" />}
          {e.keys && <CommandShortcut>{e.keys}</CommandShortcut>}
        </CommandItem>
      ))}
    </CommandGroup>
  )
}

export const matches = (needle: string, ...parts: (string | undefined)[]) => !needle || parts.join(' ').toLowerCase().includes(needle)

/** Lower-case, with `_` and `-` read as spaces, so "tariff code" finds tariff_code. */
export const plain = (s: string) => s.toLowerCase().replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim()

/** 0 = the key itself, 1 = a key prefix, 2 = a title word starts with it, 3 = anything else that matched. */
export function ticketRank(t: { key: string; title: string }, needle: string): number {
  const n = needle.trim().toLowerCase()
  if (!n) return 3
  const key = t.key.toLowerCase()
  const squash = (s: string) => plain(s).replace(/ /g, '')
  if (!squash(n)) return 3 // only dashes or underscores: nothing to rank
  if (key === n || squash(key) === squash(n)) return 0
  if (key.startsWith(n) || plain(key).startsWith(plain(n)) || squash(key).startsWith(squash(n))) return 1
  return ` ${plain(t.title)}`.includes(` ${plain(n)}`) ? 2 : 3
}
