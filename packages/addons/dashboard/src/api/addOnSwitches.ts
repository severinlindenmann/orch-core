// Settings > Add-ons (R2-2): what `GET /api/w/:ws/add-ons` answers. Words come from the server (the setup wizard's catalog).

export interface AddOnRow {
  /** The add-on's name: `start-agent`, `terminals`, `worktrees`, `publish`, `usage`, or one that is always on (`dashboard`, `quick`). */
  id: string
  /** The setting this add-on needs before it can be on (`publish`), or null. */
  needs?: 'publish' | null
  /** Whether that setting is set; null when the host could not say (then nothing is claimed). */
  set_up?: boolean | null
  /** P02: the Publish server installed on this computer (`orch install publish`), or null (hosted elsewhere): the form starts with it. */
  suggest?: string | null
  /** Where `suggest` comes from: the Publish server installed on this computer, or the hub this workspace is joined to. */
  suggest_from?: 'computer' | 'hub' | null
  suggest_hub?: string | null
  /** #1838: where files go (`server`) and where links open (`share`), as two values: from the hub's signed object (checked by core) or this computer's Publish server. */
  suggest_where?: PublishWhere | null
  /** #1838: the settings came from this hub and the hub has since moved: its new values. Sharing is paused until the person signs them. */
  hub_moved?: PublishWhere | null
  title: string
  /** The plain one-line description, the same words as the setup wizard. */
  desc: string
  /** `attention`: granted once, but its files changed (or are missing) since: approve it again. */
  state: 'on' | 'off' | 'attention'
  /** Set for an add-on that is always on and has no switch: why. */
  locked: string | null
  /** Why the switch cannot move now (needs something first, or work is running), or null. */
  blocked: string | null
  /** #1198 C14: what blocks it is dashboard execution being off (or terminals not on): the page offers to turn it on. */
  runner_switch?: boolean
  /** The add-on's version from its manifest (always-on parts: the install's own); null or absent when the host does not say. */
  version?: string | null
}

/** The two addresses of Publish. They are separate values: a hub may take files at one host and open links at another. */
export interface PublishWhere {
  server: string
  share: string
}

/**
 * What the Publish row suggests, as the pair. Only the server's row speaks: nothing here reads a URL or local state.
 * `suggest_where` first; the one-string `suggest` only when `suggest_where` is missing (an older dashboard server), for both values.
 */
export function suggestedWhere(row: Pick<AddOnRow, 'suggest' | 'suggest_where'> | undefined | null): PublishWhere | null {
  if (!row) return null
  const w = row.suggest_where
  if (w && typeof w.server === 'string' && w.server && typeof w.share === 'string' && w.share) return { server: w.server, share: w.share }
  return row.suggest ? { server: row.suggest, share: row.suggest } : null
}

export interface AddOnsData {
  available: boolean
  add_ons: AddOnRow[]
}

/** What the settings side panel needs: the schema an On add-on declares and the value the workspace holds. */
export interface AddonSettingsData {
  name: string
  title: string | null
  schema: { type?: string; properties?: Record<string, SchemaProp>; required?: string[] }
  value: Record<string, unknown> | null
}
export interface SchemaProp {
  type?: string
  title?: string
  description?: string
  enum?: (string | number)[]
  default?: unknown
  minimum?: number
  maximum?: number
  multipleOf?: number
}
