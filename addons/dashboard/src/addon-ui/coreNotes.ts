/**
 * Core's own consequence sentence for an addon action whose effect core itself carries out, so core can say it truthfully
 * (never manifest or addon text). Repos' remove and remove_anyway are core's `settings.changed` removing a name from
 * `settings.repos` (ticket format §5.4.2): that changes the declaration only, never the disk.
 */
const NOTES: Record<string, string> = {
  'repos.remove': 'The folder and its files stay on disk.',
  'repos.remove_anyway': 'The folder and its files stay on disk.',
}

export const coreNote = (addon: string | undefined, action: string | undefined): string | undefined =>
  addon && action && Object.hasOwn(NOTES, `${addon}.${action}`) ? NOTES[`${addon}.${action}`] : undefined
