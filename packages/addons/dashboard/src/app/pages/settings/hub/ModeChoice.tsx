import { violet } from './shared'

/** "What should the hub be able to see?": the two choices of a join, by click or by terminal. */
export function ModeChoice({ mode, onChange }: { mode: 'selective' | 'full' | null; onChange: (m: 'selective' | 'full') => void }) {
  return <>
    <h3 className="font-semibold">What should the hub be able to see?</h3>
    <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,220px),1fr))] gap-3" role="group" aria-label="What the hub can see">
      {(['selective', 'full'] as const).map(value => <button key={value} data-testid={'hub-mode-' + value} aria-pressed={mode === value} className={'space-y-3 rounded-lg border p-4 text-left focus-visible:ring-2 focus-visible:ring-chart-4 ' + (mode === value ? violet : 'border-border')} onClick={() => onChange(value)}>
        <strong>{value === 'selective' ? 'Only what I choose' : 'Everything'}</strong>
        <p>{value === 'selective' ? 'The hub sees the tickets you pick. Always shared: notes that need your attention, and full questions. You keep signing locally. Everything else stays on this computer.' : 'The hub can read every ticket, file and note, now and later. Hub admins act as admins here. Approvals use your phone or passkey.'}</p>
      </button>)}
    </div>
    <p>Notes that need your attention and full questions are always shared. You can allow everything later. Going back requires leaving and rejoining.</p>
  </>
}
