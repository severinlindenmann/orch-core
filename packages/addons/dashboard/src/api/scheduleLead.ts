// The minimum scheduling lead in plain words (run 13). No imports: the sign dialogs' refusal texts (click.ts) use it too.

/** The lead a real workspace has (core FU_MIN_LEAD_S): used when the server does not say. */
export const DEFAULT_MIN_LEAD_S = 3600

/** "1 hour", "2 hours", "30 minutes", "30 seconds". */
export function leadText(seconds: number): string {
  const unit = (n: number, one: string) => `${n} ${one}${n === 1 ? '' : 's'}`
  if (seconds >= 3600 && seconds % 3600 === 0) return unit(seconds / 3600, 'hour')
  if (seconds >= 60 && seconds % 60 === 0) return unit(seconds / 60, 'minute')
  return unit(seconds, 'second')
}

/** The plain sentence for a schedule that would start too soon. */
export const leadSentence = (seconds = DEFAULT_MIN_LEAD_S): string => `A schedule must start at least ${leadText(seconds)} from now.`

/** The host's `schedule.denied` for a first run inside the minimum lead, in plain words; null for anything else. */
export function scheduleDenial(code: string | undefined, message: string | undefined, seconds = DEFAULT_MIN_LEAD_S): string | null {
  return code === 'schedule.denied' && /minimum scheduling lead/.test(message ?? '') ? leadSentence(seconds) : null
}
