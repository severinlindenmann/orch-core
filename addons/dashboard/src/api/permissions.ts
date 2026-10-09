// Role permissions: the one table the mock host and the UI both read (part of the API contract).
import type { Role, Workspace } from './types'

export type Permission =
  /** Create tickets. */
  | 'ticket.create'
  /** Change a ticket at all: comment, ask, answer, approve (gate policy permitting). */
  | 'ticket.act'
  /** Answer a question addressed to someone else (the addressee may always answer). */
  | 'question.answer.any'
  /** Move a ticket to another status. */
  | 'ticket.move'
  /** Add labels to a ticket. */
  | 'ticket.label'
  /** Save a view shared with the workspace. */
  | 'view.share'
  /** Issue an agent grant for yourself, and revoke your own. */
  | 'grant.issue'
  /** Revoke anyone's agent grant. */
  | 'grant.revoke.any'
  /** Workspace settings: name, members, gate policies. */
  | 'settings'
  /** Install, grant, enable, disable, update and uninstall addons. */
  | 'addon.manage'
  /** Decide addon decisions (Today). */
  | 'addon.decide'
  /** Run addon actions (buttons, forms, commands); an action may ask for more (see the addon registry). */
  | 'addon.action'

const RANK: Record<Role, number> = { viewer: 0, member: 1, maintainer: 2, owner: 3 }

const MIN_ROLE: Record<Permission, Role> = {
  'ticket.create': 'member',
  'ticket.act': 'member',
  'question.answer.any': 'owner',
  'ticket.move': 'maintainer',
  'ticket.label': 'maintainer',
  'view.share': 'member',
  'grant.issue': 'maintainer',
  'grant.revoke.any': 'owner',
  settings: 'owner',
  'addon.manage': 'owner',
  'addon.decide': 'maintainer',
  'addon.action': 'member',
}

/** Does `role` rank at least `min`? No role (not a member) meets nothing. */
export const atLeast = (role: Role | undefined, min: Role): boolean => !!role && RANK[role] >= RANK[min]

/** May a person with `role` (undefined: not a member) do `permission`? */
export const can = (role: Role | undefined, permission: Permission): boolean => atLeast(role, MIN_ROLE[permission])

/** Owners revoke any grant; whoever may issue grants revokes their own. */
export const canRevokeGrant = (role: Role | undefined, grantPerson: string, person: string): boolean =>
  can(role, 'grant.revoke.any') || (can(role, 'grant.issue') && grantPerson === person)

/** A person's role in a workspace; undefined when they are not a member. */
export const roleOf = (workspace: Pick<Workspace, 'members'> | undefined, person: string | undefined): Role | undefined =>
  person ? workspace?.members.find((m) => m.person === person)?.role : undefined
