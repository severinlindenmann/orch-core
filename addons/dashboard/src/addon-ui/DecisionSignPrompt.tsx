import type { AddonDecision } from '@/api/types'
import { SignPrompt } from '@/components/sign/SignPrompt'
import { AddonBadge } from './AddonBadge'
import { plain } from '@/components/sign/visible'
import { addonName, Raw, TERMS_LEAD, words } from './SignConfirm'
import { useAddons } from './slots'

/**
 * Core's signing prompt for an addon decision (Today's cards and any addon surface that posts a decision action).
 * Every fact comes from the decision core holds (GET addon-decisions), never from the addon node's args.
 * Trust split (as SignConfirm): the title and the covers are core's own words about exactly what is posted (the
 * addon, the decision id, the option key, the ticket, the workspace). What the addon wrote (the decision's title,
 * question, detail and the option's label) is shown apart, labelled, in the dashed "From the addon" region.
 * Only after signing is the action posted with core's `confirmed` flag; the host refuses a decision without it.
 */
export function DecisionSignPrompt({ d, option, workspacePrefix, onSign, onClose, changed }: { d: AddonDecision; option: AddonDecision['options'][number]; workspacePrefix: string; onSign: () => void; onClose: () => void; changed?: boolean }) {
  // The addon by its display name (its manifest says it, so it could say anything) and always its package id.
  const { data: packages } = useAddons()
  const title = packages?.find((p) => p.name === d.addon)?.title ?? d.addon
  const named = addonName(title, d.addon)
  return (
    <SignPrompt
      title={decisionTitle(title, d.addon)}
      covers={[
        <>
          Decision <Raw>{d.id}</Raw>
        </>,
        // What answering authorises, one core line per term ("Words (key): value"), in full; posted with the answer.
        // The terms are the addon's statements, bound by core: a core lead line says so before them.
        ...(d.terms && Object.keys(d.terms).length ? [TERMS_LEAD] : []),
        ...Object.entries(d.terms ?? {}).map(([k, v]) => (
          <span key={`term-${k}`} data-term-key={k} data-term-value={String(v)}>
            {words(k) === k ? <Raw>{k}</Raw> : <>{plain(words(k))} (<Raw>{k}</Raw>)</>}: <Raw>{String(v)}</Raw>
          </span>
        )),
        <>
          Answer: option <Raw>{option.key}</Raw>
        </>,
        ...(d.ticket ? [`About ${d.ticket}`] : []),
        `In workspace ${workspacePrefix}`,
      ]}
      confirmLabel="Send answer"
      onClose={onClose}
      onSign={onSign}
    >
      {changed && (
        <p role="alert" className="text-[13px] text-warning">
          This decision changed after you opened this prompt. You sign what is shown here; orch refuses it if it no longer matches. Close and reopen it to see the current one.
        </p>
      )}
      {/* Shown in full, never cut (a permit's question carries the exact command): long text wraps and scrolls in the box. */}
      <section aria-label={`From addon ${d.addon}`} className="max-h-[40vh] space-y-1 overflow-auto rounded-md border border-dashed border-border p-2 text-[13px] text-text-muted">
        <p className="flex items-center gap-1.5 text-[12px]">
          <AddonBadge name={d.addon} title={title} />
          <span>From the addon {named}</span>
        </p>
        <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">
          Title: <span className="text-text">{d.title}</span>
        </p>
        <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">
          Question: <span className="text-text">{d.question}</span>
        </p>
        <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">
          Option <Raw>{option.key}</Raw> is labelled: <span className="text-text">{option.label}</span>
        </p>
        {d.detail && <p className="whitespace-pre-wrap text-text [overflow-wrap:anywhere]">{d.detail}</p>}
      </section>
    </SignPrompt>
  )
}

/** The dialog title: core's words and the addon's name only ("Decide for Publish (publish)"). */
export const decisionTitle = (title: string, addon: string) => `Decide for ${addonName(title, addon)}`

/** The confirmation toast: core's sentence as the title, the addon's own answer as the labelled description. */
export const decisionToast = (title: string, addon: string, option: string, message?: string) => ({
  signedToast: true as const,
  message: `Signed: answer ${plain(option)} · ${addonName(title, addon)}`,
  ...(message ? { description: `Addon says: ${message}` } : {}),
})

/** Has the live decision moved away from the one a prompt was opened on (id, terms or options)? */
export const decisionChanged = (opened: AddonDecision, live: AddonDecision | undefined) =>
  !live || live.id !== opened.id || JSON.stringify(live.terms ?? null) !== JSON.stringify(opened.terms ?? null) || JSON.stringify(live.options) !== JSON.stringify(opened.options)

/** The body core posts for a decision after its prompt: the decision's own id, ticket and terms, the option key, `confirmed`. */
export const decisionBody = (d: AddonDecision, option: string) => ({ id: d.id, option, ...(d.ticket ? { ticket: d.ticket } : {}), ...(d.terms ? { terms: { ...d.terms } } : {}), confirmed: true })
