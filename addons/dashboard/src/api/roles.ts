import type { Role } from './types'

/** Gate approver groups: 'maintainer' means owners and maintainers; any other value is an exact role. */
export const roleMeets = (role: Role | string, approvers: string): boolean =>
  approvers === 'maintainer' ? role === 'owner' || role === 'maintainer' : role === approvers
