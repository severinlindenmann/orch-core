import { InfoDrawer } from '@/components/InfoDrawer'
import { violet } from './shared'

/** "What the hub can see": the two choices of a join, by click or by terminal. One short line each; the full text is in the "i". */
export function ModeChoice({ mode, onChange }: { mode: 'selective' | 'full' | null; onChange: (m: 'selective' | 'full') => void }) {
  return <>
    <div className="flex items-center gap-2"><h3 className="font-semibold">What the hub can see</h3>
      <InfoDrawer testId="hub-mode-info"><p>Only what I choose: the hub sees the tickets you pick. Notes that need your attention and full questions are always shared. You keep signing locally. Everything else stays on this computer.</p><p>Everything: the hub can read every ticket, file and note, now and later. Hub admins act as admins here. Approvals use your phone or passkey.</p></InfoDrawer></div>
    <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,220px),1fr))] gap-3" role="group" aria-label="What the hub can see">
      {(['selective', 'full'] as const).map(value => <button key={value} data-testid={'hub-mode-' + value} aria-pressed={mode === value} className={'space-y-1 rounded-lg border p-3 text-left focus-visible:ring-2 focus-visible:ring-chart-4 ' + (mode === value ? violet : 'border-border')} onClick={() => onChange(value)}>
        <strong>{value === 'selective' ? 'Only what I choose' : 'Everything'}</strong>{value === 'selective' && <span className="text-sm text-text-muted"> · recommended</span>}
        <p className="text-sm text-text-muted">{value === 'selective' ? 'You pick the tickets. Everything else stays on this computer.' : 'The hub can read every ticket and file.'}</p>
      </button>)}
    </div>
  </>
}
