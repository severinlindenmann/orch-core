// The one date and time formatter (R2, A m8 / B m13). Every page and every addon view says times the same way:
//  - an event within the last 7 days is relative: "just now", "5 min ago", "3 h ago", "2 days ago";
//  - older ones (and any absolute time) are "9 Oct 11:36";
//  - a time in the future (a clock a little ahead) reads "just now", never "in 6 min";
//  - "UTC" is written only where the exact instant matters (Details, tooltips): fmtExact, "9 Oct 2026 11:36 UTC";
//  - deadlines and slots that are a time of day ("until 17:00") use fmtClock.
// Times are shown in UTC (the host's clock); the suffix is left off everywhere but fmtExact.
// Pure: no React, no store. "now" is the host's clock (the mock's in this mockup), registered by src/api/client.ts.

const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const DAY_MS = 86_400_000
const pad = (n: number) => String(n).padStart(2, '0')

let clock: () => number = () => Date.now()

/** The client registers the host's clock (the mock store's "now"), so relative times match the data. */
export function setClock(fn: () => number): void {
  clock = fn
}

/** The host's "now" in ms. */
export function nowMs(): number {
  return clock()
}

const toMs = (t: string | number | undefined): number => (t === undefined ? clock() : typeof t === 'number' ? t : Date.parse(t))
const parse = (iso: string): Date | null => {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? null : d
}

/** "1 task", "3 tasks"; `many` for irregular plurals. */
export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`
}

/** "11:36" (a time of day: deadlines, slots, "until 17:00"). */
export function fmtClock(iso: string): string {
  const d = parse(iso)
  return d ? `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}` : iso
}

/** "9 Oct". */
export function fmtDay(iso: string): string {
  const d = parse(iso)
  return d ? `${d.getUTCDate()} ${MON[d.getUTCMonth()]}` : iso
}

/** "9 Oct 11:36": an absolute time. */
export function fmtDateTime(iso: string): string {
  const d = parse(iso)
  return d ? `${fmtDay(iso)} ${fmtClock(iso)}` : iso
}

/** "9 Oct 2026 11:36 UTC": the exact instant, for Details and tooltips only. */
export function fmtExact(iso: string): string {
  const d = parse(iso)
  return d ? `${fmtDay(iso)} ${d.getUTCFullYear()} ${fmtClock(iso)} UTC` : iso
}

/** A duration in words: "45 s", "5 min", "2 h 5 min", "3 days". */
export function fmtSpan(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000))
  if (s < 60) return `${s} s`
  const min = Math.round(s / 60)
  if (min < 60) return `${min} min`
  if (min < 24 * 60) {
    const h = Math.floor(min / 60)
    return min % 60 ? `${h} h ${min % 60} min` : `${h} h`
  }
  return plural(Math.round(min / 1440), 'day')
}

/** When something happened: relative within 7 days ("5 min ago"), else "9 Oct 11:36"; the future is "just now". */
export function fmtWhen(iso: string, now?: string | number): string {
  const at = Date.parse(iso)
  if (Number.isNaN(at)) return iso
  const diff = toMs(now) - at
  if (diff < 60_000) return 'just now'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} min ago`
  if (diff < DAY_MS) return `${Math.floor(diff / 3_600_000)} h ago`
  if (diff < 7 * DAY_MS) return `${plural(Math.floor(diff / DAY_MS), 'day')} ago`
  return fmtDateTime(iso)
}

/** How long something has waited ("14 min", "2 h", "3 days"); a start in the future is "just now". */
export function fmtAge(from: string, now?: string | number): string {
  const diff = toMs(now) - Date.parse(from)
  if (!(diff >= 60_000)) return 'just now'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} min`
  if (diff < DAY_MS) return `${Math.floor(diff / 3_600_000)} h`
  return plural(Math.floor(diff / DAY_MS), 'day')
}
