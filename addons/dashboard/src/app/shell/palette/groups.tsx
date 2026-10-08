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
