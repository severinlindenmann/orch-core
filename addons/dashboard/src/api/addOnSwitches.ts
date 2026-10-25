// Settings > Add-ons (R2-2): what `GET /api/w/:ws/add-ons` answers. Words come from the server (the setup wizard's catalog).

export interface AddOnRow {
  /** The add-on's name: `start-agent`, `terminals`, `worktrees`, `publish`, `usage`, or one that is always on (`dashboard`, `quick`). */
  id: string
  /** The setting this add-on needs before it can be on (`publish`), or null. */
  needs?: 'publish' | null
  /** Whether that setting is set; null when the host could not say (then nothing is claimed). */
  set_up?: boolean | null
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

export interface AddOnsData {
  available: boolean
  add_ons: AddOnRow[]
}
