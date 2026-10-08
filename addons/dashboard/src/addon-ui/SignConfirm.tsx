import { SignPrompt } from '@/components/sign/SignPrompt'
import { AddonBadge } from './AddonBadge'

const MAX = 120
const cap = (v: unknown) => {
  const t = String(v)
  return t.length > MAX ? `${t.slice(0, MAX)}…` : t
}

/**
 * Core's signing prompt for an addon action the manifest marks `confirm: 'sign'` (arm a schedule, pause the factory).
 * The title is the manifest's label, the covers are core's own words; what the addon sent with the action (an id) is
 * shown apart, as capped plain text. Only core posts the action afterwards, with its `confirmed` flag.
 */
export function SignConfirm({ addon, label, args, onSign, onClose }: { addon: string; label: string; args?: Record<string, unknown>; onSign: () => void; onClose: () => void }) {
  const sent = Object.entries(args ?? {}).filter(([, v]) => typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean')
  return (
    <SignPrompt title={label} covers={[`${label}, in this workspace`, 'Signed as you, with your own key']} confirmLabel="Sign with Touch ID" onSign={onSign} onClose={onClose}>
      <section aria-label={`From addon ${addon}`} className="space-y-1 text-[13px] text-text-muted">
        <p className="flex items-center gap-1.5">
          <AddonBadge name={addon} />
          Requested by addon <span className="font-mono">{addon}</span>
          {sent.length > 0 ? ':' : '.'}
        </p>
        {sent.map(([k, v]) => (
          <p key={k} className="break-all font-mono text-[12px] text-text">
            {cap(k)} = {cap(v)}
          </p>
        ))}
      </section>
    </SignPrompt>
  )
}
