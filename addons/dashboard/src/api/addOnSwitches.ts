// Settings > Add-ons (R2-2): what `GET /api/w/:ws/add-ons` answers. Words come from the server (the setup wizard's catalog).

export interface AddOnRow {
  /** The add-on's name: `start-agent`, `terminals`, `worktrees`, `publish`, `model-routing`, or one that is always on (`dashboard`, `quick`). */
  id: string
  title: string
  /** The plain one-line description, the same words as the setup wizard. */
  desc: string
  /** `attention`: granted once, but its files changed (or are missing) since: approve it again. */
  state: 'on' | 'off' | 'attention'
  /** Set for an add-on that is always on and has no switch: why. */
  locked: string | null
  /** Why the switch cannot move now (needs something first, or work is running), or null. */
  blocked: string | null
}

export interface AddOnsData {
  available: boolean
  add_ons: AddOnRow[]
}
