/**
 * Core's own consequence sentence for an addon action whose effect core itself carries out, so core can say it truthfully
 * (never manifest or addon text). Repos' remove and remove_anyway are core's `settings.changed` removing a name from
 * `settings.repos` (ticket format §5.4.2): that changes the declaration only, never the disk. Factory's zero-hold
 * consequences depend on the signed args: a new run still needs its checks, a re-delivery has already passed them.
 */
type Note = string | ((args: Record<string, unknown>) => string | undefined)
const NOTES: Record<string, Note> = {
  'factory.start_run': args => args.goes_up_to === 'Deliver' && args.hold_minutes === 0 ? 'No hold: delivers as soon as its checks pass' : undefined,
  'factory.redeliver': args => args.hold_minutes === 0 ? 'Delivers as soon as you sign' : undefined,
  'repos.remove': 'The folder and its files stay on disk.',
  'repos.remove_anyway': 'The folder and its files stay on disk.',
}

export function coreNote(addon: string | undefined, action: string | undefined, args: Record<string, unknown> = {}): string | undefined {
  const note = addon && action && Object.hasOwn(NOTES, `${addon}.${action}`) ? NOTES[`${addon}.${action}`] : undefined
  return typeof note === 'function' ? note(args) : note
}
