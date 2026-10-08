// Pure workspace rules shared by the mock and the UI (part of the API contract).
import type { Workspace } from './types'

/** The home workspace of a ticket, by its key prefix (`DEMO-0043` -> DEMO). Undefined when no workspace has that prefix. */
export function workspaceOfTicket<W extends Pick<Workspace, 'prefix'>>(key: string, workspaces: readonly W[]): W | undefined {
  const dash = key.lastIndexOf('-')
  if (dash <= 0) return undefined
  const prefix = key.slice(0, dash)
  return workspaces.find((w) => w.prefix === prefix)
}
