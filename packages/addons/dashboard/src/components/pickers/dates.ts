/** Local-date helpers for the pickers. Values are plain local strings: "YYYY-MM-DD" and "HH:MM". */
const p2 = (n: number) => String(n).padStart(2, '0')
export const toDateStr = (d: Date) => `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`
export function parseDateStr(s: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s)
  if (!m) return null
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]))
  return toDateStr(d) === s ? d : null
}
export const addDays = (d: Date, n: number) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n)
/** The next Monday strictly after `d`. */
export const nextMonday = (d: Date) => addDays(d, ((8 - d.getDay()) % 7) || 7)
export function dateChips(now: Date): { label: string; value: string }[] {
  return [
    { label: 'Today', value: toDateStr(now) },
    { label: 'Tomorrow', value: toDateStr(addDays(now, 1)) },
    { label: 'Next Monday', value: toDateStr(nextMonday(now)) },
  ]
}
export function dateLabel(s: string, now: Date): string {
  const d = parseDateStr(s)
  if (!d) return ''
  const hit = dateChips(now).find((c) => c.value === s)
  if (hit) return hit.label
  return d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short', year: d.getFullYear() === now.getFullYear() ? undefined : 'numeric' })
}
export const TIME_CHIPS = [
  { label: 'Morning', value: '09:00' },
  { label: 'Noon', value: '12:00' },
  { label: 'Afternoon', value: '15:00' },
  { label: 'Evening', value: '18:00' },
]
export function parseTime(s: string): number | null {
  const m = /^(\d{2}):(\d{2})$/.exec(s)
  if (!m) return null
  const h = Number(m[1]), mi = Number(m[2])
  return h < 24 && mi < 60 ? h * 60 + mi : null
}
export const fmtTime = (mins: number) => { const m = ((mins % 1440) + 1440) % 1440; return `${p2(Math.floor(m / 60))}:${p2(m % 60)}` }
/** The last step of the day (23:45 for 15 minutes): the stepper never wraps past midnight. */
export const lastStep = (step: number) => 1440 - step
/** `mins` rounded up to the next `step`, never past the last step of the day. */
export const ceilStep = (mins: number, step: number) => Math.min(Math.ceil(mins / step) * step, lastStep(step))
export const clampTime = (mins: number, lo: number, hi: number) => Math.min(Math.max(mins, lo), hi)
