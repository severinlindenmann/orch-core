import { SignPrompt } from '@/components/sign/SignPrompt'
import { AddonBadge } from './AddonBadge'

const MAX = 120
/** "arm_schedule" → "Arm schedule": an id said in words (core's own rendering of the action id and arg names). */
export const words = (id: string) => {
  const t = id.replace(/[_.-]+/g, ' ').trim()
  return t ? t.charAt(0).toUpperCase() + t.slice(1) : id
}
/** The exact value that is signed, verbatim and in mono: never faded, never replaced by its words. */
export const Raw = ({ children }: { children: string }) => <code className="break-all font-mono text-[12px] text-text">{children}</code>
/** Words plus the exact id whenever the words differ from it ("Arm schedule (arm_schedule)"): two ids never read alike. */
export const wordsAndId = (id: string) => (words(id) === id ? id : `${words(id)} (${id})`)
const cap = (v: unknown) => {
  const t = String(v)
  return t.length > MAX ? `${t.slice(0, MAX)}…` : t
}
const scalar = (v: unknown) => typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean'

/**
 * Why core cannot show these args exactly as they would be posted (null when it can). A signature covers every arg
 * the host receives, so an arg that is not a plain value, or a key or value longer than the dialog shows, fails
 * closed: nothing is signed or posted.
 */
export function signArgsProblem(args: Record<string, unknown> = {}): string | null {
  for (const [k, v] of Object.entries(args)) {
    if (!scalar(v)) return `The addon sent "${cap(k)}" as a value core cannot show, so nothing was signed or sent.`
    if (k.length > MAX || String(v).length > MAX) return `The addon sent "${cap(k)}" longer than core shows (${MAX} characters), so nothing was signed or sent.`
  }
  return null
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
  ticket,
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
  /** The ticket in core's render context: posted as `ticket`, so it is covered. */
  ticket?: string
  onSign: () => void
  onClose: () => void
}) {
  // Every arg the host receives is shown; anything core could not show exactly blocks the signature.
  const problem = signArgsProblem(args)
  const sent = problem ? [] : Object.entries(args ?? {})
  return (
    <SignPrompt
      title={signTitle(action, addonTitle, addon)}
      covers={[
        <>
          Runs "{words(action)}" (<Raw>{action}</Raw>) of the addon {addonTitle === addon ? <Raw>{addon}</Raw> : <>{addonTitle} (<Raw>{addon}</Raw>)</>}
        </>,
        ...(ticket ? [`About ${ticket}`] : []),
        `In workspace ${workspace.name} (${workspace.prefix})`,
      ]}
      confirmLabel="Sign and run"
      disabled={!!problem}
      onSign={onSign}
      onClose={onClose}
    >
      <FromAddon addon={addon} addonTitle={addonTitle} label={label} subject={subject} args={Object.fromEntries(sent)} />
      {problem && (
        <p role="alert" className="text-[13px] text-danger">
          {problem}
        </p>
      )}
    </SignPrompt>
  )
}

/**
 * The dashed "From the addon" region every core confirm and signing dialog uses for what the addon wrote: its label,
 * its sentence, the row's name ("Addon says:") and the args it picked (words, exact key, exact value). Capped plain text.
 */
export function FromAddon({ addon, addonTitle, label, text, subject, args }: { addon: string; addonTitle: string; label?: string; text?: string; subject?: string; args?: Record<string, unknown> }) {
  return (
    <section aria-label={`From addon ${addon}`} className="space-y-1 rounded-md border border-dashed border-border p-2 text-[13px] text-text-muted">
      <p className="flex items-center gap-1.5">
        <AddonBadge name={addon} title={addonTitle} />
        <span>
          From the addon {addonTitle === addon ? <Raw>{addon}</Raw> : <>{addonTitle} (<Raw>{addon}</Raw>)</>}
        </span>
      </p>
      {label && <p className="break-words text-text">{cap(label)}</p>}
      {text && <p className="break-words text-text">{cap(text)}</p>}
      {subject && (
        <p className="break-words text-text-muted">
          Addon says: <span className="text-text">{cap(subject)}</span>
        </p>
      )}
      {/* What is sent: each arg's words, its exact key when they differ, and the exact value. */}
      {Object.entries(args ?? {}).map(([k, v]) => (
        <p key={k} className="break-words text-[13px] text-text">
          {words(k) === k ? cap(k) : <>{cap(words(k))} (<Raw>{cap(k)}</Raw>)</>}: <Raw>{cap(v)}</Raw>
        </p>
      ))}
    </section>
  )
}

/** The dialog title and toast title: core's words only. */
export const signTitle = (action: string, addonTitle: string, addon: string) => `Sign: ${wordsAndId(action)} · ${addonName(addonTitle, addon)}`
/** "Schedules (schedules)": the manifest title and always the package id, once when they are the same. */
export const addonName = (title: string, id: string) => (title === id ? id : `${title} (${id})`)
