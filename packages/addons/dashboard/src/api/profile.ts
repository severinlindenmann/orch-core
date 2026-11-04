// The workspace profile (hub-full HUB-WS-09, #1390): what this workspace does, written once by its owner and shown in
// the hub (people read it, agents use it to find the right workspace). Same closed fields as the host's schema
// (core `host/hubops.py`); a change is an owner click, not a signed event. The host, not this page, sends it to a hub.
import { queryOptions } from '@tanstack/react-query'
import { api } from '@/api/client'

export interface ProfileFields {
  listed: boolean
  tags: string[]
  handles: string[]
  not_handled: string[]
  accepts: string[]
  description: string
  contact: string
  languages: string
  hours: string
}

/** The answer: the fields as the host has them, and whether the hub has them now. */
export type WorkspaceProfile = ProfileFields & { pushed: boolean }

export const EMPTY_PROFILE: ProfileFields = {
  listed: false, tags: [], handles: [], not_handled: [], accepts: [], description: '', contact: '', languages: '', hours: '',
}

export const PROFILE_LIMITS = { items: 8, tag: 24, handle: 80, accept: 24, description: 300, contact: 80, languages: 40, hours: 40 } as const

/** The kinds of request a workspace may accept, to pick from (the host takes any short word). */
export const ACCEPT_CHOICES = ['question', 'ticket', 'review', 'approval'] as const

/** Only the closed fields, trimmed, empty items dropped: what the page sends. */
export function cleanProfile(p: ProfileFields): ProfileFields {
  const list = (v: string[], max: number) => v.map((x) => x.trim().slice(0, max)).filter(Boolean).slice(0, PROFILE_LIMITS.items)
  return {
    listed: p.listed,
    tags: list(p.tags, PROFILE_LIMITS.tag),
    handles: list(p.handles, PROFILE_LIMITS.handle),
    not_handled: list(p.not_handled, PROFILE_LIMITS.handle),
    accepts: list(p.accepts, PROFILE_LIMITS.accept),
    description: p.description.trim().slice(0, PROFILE_LIMITS.description),
    contact: p.contact.trim().slice(0, PROFILE_LIMITS.contact),
    languages: p.languages.trim().slice(0, PROFILE_LIMITS.languages),
    hours: p.hours.trim().slice(0, PROFILE_LIMITS.hours),
  }
}

export const profileQueries = {
  get: (ws: string) => queryOptions({ queryKey: ['hub', ws, 'profile'], queryFn: () => api.getHubProfile(ws), retry: false }),
}
