import { ARG_KEY } from '@/api/addons'
import { SignPrompt } from '@/components/sign/SignPrompt'
import { plain, Raw } from '@/components/sign/visible'
import { AddonBadge } from './AddonBadge'

export { Raw } from '@/components/sign/visible'

/** Core's lead line before the arg lines of a signature: the values are the addon's, and they are sent exactly as shown. */
export const ARGS_LEAD = 'Values set by the addon (sent exactly as shown):'
/** Core's lead line before a decision's term lines: the terms are the addon's, and the host checks them again on the answer. */
export const TERMS_LEAD = 'Terms set by the addon (checked again when you answer):'
/** At most this many args are signed in one action; more are refused (never silently dropped). */
export const MAX_SIGNED_ARGS = 12
/** "arm_schedule" → "Arm schedule": an id said in words (core's own rendering of the action id and arg names). */
export const words = (id: string) => {
  const t = id.replace(/[_.-]+/g, ' ').trim()
  return t ? t.charAt(0).toUpperCase() + t.slice(1) : id
}
/** Words plus the exact id whenever the words differ from it ("Arm schedule (arm_schedule)"): two ids never read alike. Invisible characters shown. */
export const wordsAndId = (id: string) => (words(id) === id ? plain(id) : `${plain(words(id))} (${plain(id)})`)
const scalar = (v: unknown) => typeof v === 'string' || typeof v === 'boolean' || (typeof v === 'number' && Number.isFinite(v))

/**
 * Why core cannot show these args exactly as they would be posted (null when it can). A signature covers every arg
 * the host receives, so an arg that is not a plain value (an object, NaN, Infinity), or more args than core signs at
 * once, fails closed: nothing is signed or posted. Long plain values are shown in full, never cut.
 */
export function signArgsProblem(args: Record<string, unknown> = {}): string | null {
  const entries = Object.entries(args)
  if (entries.length > MAX_SIGNED_ARGS) return `The addon sent ${entries.length} values; core signs at most ${MAX_SIGNED_ARGS} at once, so nothing was signed or sent.`
  for (const [k, v] of entries) {
    if (!ARG_KEY.test(k)) return `The addon sent a value under the key "${plain(k)}", which core does not accept, so nothing was signed or sent.`
    if (!scalar(v)) return `The addon sent "${plain(k)}" as a value core cannot show, so nothing was signed or sent.`
  }
  return null
}

/**
 * Core's lines for the args that are sent, one per arg: "Words (key): value", key and value exact (Raw). In core's own
 * area (the covers, or the list above the addon region), never inside the addon's region where its text could imitate them.
 */
export function argLines(args: Record<string, unknown> = {}) {
  return Object.entries(args).map(([k, v]) => (
    <span key={k} data-arg-key={k} data-arg-value={String(v)}>
      {words(k) === k ? <Raw>{k}</Raw> : <>{plain(words(k))} (<Raw>{k}</Raw>)</>}: <Raw>{String(v)}</Raw>
    </span>
  ))
}

/**
 * Core's signing prompt for an addon action the manifest marks `confirm: 'sign'` (arm a schedule, pause the factory).
 * Trust split (as for the start dialog): the title and the covers are core's own words, built from what core knows
 * (the action id, the addon's title, the workspace, the ticket and every arg that is sent). What the addon wrote about
 * it (the manifest label, the row's name) is shown apart, in full, in the dashed "From addon" region. Only core posts the
 * action afterwards.
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
  // Every arg the host receives is a cover line; anything core could not show exactly blocks the signature.
  const problem = signArgsProblem(args)
  return (
    <SignPrompt
      title={signTitle(action, addonTitle, addon)}
      covers={[
        <>
          Runs "{plain(words(action))}" (<Raw>{action}</Raw>) of the addon {addonTitle === addon ? <Raw>{addon}</Raw> : <>{plain(addonTitle)} (<Raw>{addon}</Raw>)</>}
        </>,
        // The values below are the addon's, bound by core: a core lead line says so before them.
        ...(problem || !Object.keys(args ?? {}).length ? [] : [ARGS_LEAD, ...argLines(args)]),
        ...(ticket ? [`About ${plain(ticket)}`] : []),
        `In workspace ${workspace.name} (${workspace.prefix})`,
      ]}
      confirmLabel="Sign and run"
      disabled={!!problem}
      onSign={onSign}
      onClose={onClose}
    >
      <FromAddon addon={addon} addonTitle={addonTitle} label={label} subject={subject} />
      {problem && (
        <p role="alert" className="text-[13px] text-danger">
          {problem}
        </p>
      )}
    </SignPrompt>
  )
}

/**
 * The dashed "From the addon" region every core confirm and signing dialog uses for what the addon wrote about the
 * action: its label, its sentence and the row's name ("Addon says:"). Shown in full as plain text (wrapped, the box
 * scrolls when tall), labelled as the addon's. The args that are sent are never here: they are core's lines.
 */
export function FromAddon({ addon, addonTitle, label, text, subject }: { addon: string; addonTitle: string; label?: string; text?: string; subject?: string }) {
  return (
    // Everything in full, never cut: long text wraps, and the box scrolls when it is taller than 40 % of the screen.
    <section aria-label={`From addon ${addon}`} className="max-h-[40vh] space-y-1 overflow-auto rounded-md border border-dashed border-border p-2 text-[13px] text-text-muted">
      <p className="flex items-center gap-1.5">
        <AddonBadge name={addon} title={addonTitle} />
        <span>
          From the addon {addonTitle === addon ? <Raw>{addon}</Raw> : <>{plain(addonTitle)} (<Raw>{addon}</Raw>)</>}
        </span>
      </p>
      {label && <p className="whitespace-pre-wrap text-text [overflow-wrap:anywhere]">{label}</p>}
      {text && <p className="whitespace-pre-wrap text-text [overflow-wrap:anywhere]">{text}</p>}
      {subject && (
        <p className="whitespace-pre-wrap text-text-muted [overflow-wrap:anywhere]">
          Addon says: <span className="text-text">{subject}</span>
        </p>
      )}
    </section>
  )
}

/** The dialog title and toast title: core's words only. */
export const signTitle = (action: string, addonTitle: string, addon: string) => `Sign: ${wordsAndId(action)} · ${addonName(addonTitle, addon)}`
/** "Schedules (schedules)": the manifest title and always the package id, once when they are the same. Invisible characters shown. */
export const addonName = (title: string, id: string) => (title === id ? plain(id) : `${plain(title)} (${plain(id)})`)
