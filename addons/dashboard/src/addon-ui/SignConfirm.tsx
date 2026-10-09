import { SignPrompt } from '@/components/sign/SignPrompt'
import { AddonBadge } from './AddonBadge'

const MAX = 120
/** "arm_schedule" → "Arm schedule": an id said in words (core's own rendering of the action id and arg names). */
export const words = (id: string) => {
  const t = id.replace(/[_.-]+/g, ' ').trim()
  return t ? t.charAt(0).toUpperCase() + t.slice(1) : id
}
/** "Schedules" → "Schedule": what one item of the addon is called, for the line naming the item an id points at. */
const singular = (title: string) => (title.endsWith('s') && !title.endsWith('ss') ? title.slice(0, -1) : title)
const cap = (v: unknown) => {
  const t = String(v)
  return t.length > MAX ? `${t.slice(0, MAX)}…` : t
}

/**
 * Core's signing prompt for an addon action the manifest marks `confirm: 'sign'` (arm a schedule, pause the factory).
 * Trust split (as for the start dialog): the title and the covers are core's own words, built from what core knows
 * (the action id, the addon's title, the workspace). Everything the addon wrote (the manifest label, the args it sent)
 * is shown apart, as capped plain text, in the dashed "From addon" region. Only core posts the action afterwards.
 */
export function SignConfirm({
  addon,
  addonTitle,
  action,
  workspace,
  label,
  args,
  subject,
  onSign,
  onClose,
}: {
  addon: string
  addonTitle: string
  action: string
  workspace: { prefix: string; name: string }
  label?: string
  args?: Record<string, unknown>
  /** What the action is about as the row names it ("Check inbox"). The addon wrote it: shown as extra context, labelled, never instead of the args that are signed. */
  subject?: string
  onSign: () => void
  onClose: () => void
}) {
  const sent = Object.entries(args ?? {}).filter(([, v]) => typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean')
  // The item an `id` arg points at is named by the row it came from ("Schedule: Smoke on testing"); the id stays below.
  const named = subject && sent.some(([k]) => k === 'id')
  return (
    <SignPrompt
      title={signTitle(action, addonTitle)}
      covers={[`Runs "${words(action)}" of the addon ${addonTitle}`, `In workspace ${workspace.name} (${workspace.prefix})`]}
      confirmLabel="Sign and run"
      onSign={onSign}
      onClose={onClose}
    >
      <section aria-label={`From addon ${addon}`} className="space-y-1 rounded-md border border-dashed border-border p-2 text-[13px] text-text-muted">
        <p className="flex items-center gap-1.5">
          <AddonBadge name={addon} title={addonTitle} />
          From the addon {addonTitle}
        </p>
        {label && <p className="break-words text-text">{cap(label)}</p>}
        {subject && (
          <p className="break-words text-text-muted">
            {named ? `${singular(addonTitle)}: ` : 'About: '}
            <span className="text-text">{cap(subject)}</span>
          </p>
        )}
        {sent.map(([k, v]) => (
          <p key={k} className={named && k === 'id' ? 'break-all text-[12px] text-text-faint' : 'break-words text-[13px] text-text'}>
            {named && k === 'id' ? 'Id' : cap(words(k))}: <span className={typeof v === 'string' && /^[a-z0-9_.-]+$/i.test(v) ? 'font-mono text-[12px]' : undefined}>{cap(v)}</span>
          </p>
        ))}
      </section>
    </SignPrompt>
  )
}

/** The dialog title and toast title: core's words only. */
export const signTitle = (action: string, addonTitle: string) => `Sign: ${words(action)} · ${addonTitle}`
