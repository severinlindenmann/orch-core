// Which shared spaces this person has already opened (run-15c P03): kept per person and workspace in this browser, like
// the sidebar pins. Only Today's "New shared space" row reads it; nothing here is sent anywhere.
const KEY = (person: string, ws: string) => `orch.spaces.seen.${person}.${ws}`

export function readSeenSpaces(person: string | undefined, ws: string | undefined): string[] {
  if (!person || !ws) return []
  try {
    const v: unknown = JSON.parse(localStorage.getItem(KEY(person, ws)) ?? 'null')
    return Array.isArray(v) && v.every((x) => typeof x === 'string') ? v : []
  } catch {
    return []
  }
}

/** Mark spaces as opened. Storage can be blocked: then the row comes back after a reload, which is harmless. */
export function markSpacesSeen(person: string | undefined, ws: string | undefined, ids: string[]) {
  if (!person || !ws || !ids.length) return
  const next = [...new Set([...readSeenSpaces(person, ws), ...ids])]
  try {
    localStorage.setItem(KEY(person, ws), JSON.stringify(next))
  } catch {
    /* storage unavailable */
  }
}
