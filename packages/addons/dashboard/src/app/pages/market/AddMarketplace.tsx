// "+ Add marketplace" (#1237 pick 4.1a): a picker of the sources the host offers, one Add click then one Approve click.
// An address field exists only behind a closed fold and only where the workspace policy allows any marketplace
// (the host says so with `open_address`); otherwise the plain policy message and a way to the workspace admins.
import { Link } from '@tanstack/react-router'
import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import { marketError, type Market, type MarketList } from '@/api/market'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'

const repo = (url: string) => url.replace(/^https:\/\/github\.com\//, '')
const CLASS_WORD: Record<string, string> = { official: 'Signed by orch', dev: 'Dev sandbox only', company: 'Your company' }

export function AddMarketplace({ ws, list, markets, onAdded }: { ws: string; list: MarketList | undefined; markets: Market[]; onAdded: (url: string) => void }) {
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [picked, setPicked] = useState<string | null>(null)
  const [address, setAddress] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const offered = list?.offered ?? []
  const anySource = !!list?.open_address
  const allowed = offered.length > 0 || anySource
  const close = () => {
    setOpen(false)
    setPicked(null)
    setError('')
  }
  const add = async (url: string) => {
    setBusy(true)
    setError('')
    try {
      await api.marketAdd(ws, url)
      await qc.invalidateQueries({ queryKey: ['market', ws] })
      close()
      onAdded(url)
    } catch (e) {
      setError(marketError(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="space-y-1">
      <Button disabled={!allowed} aria-describedby={allowed ? undefined : 'add-market-why'} onClick={() => setOpen(true)}>
        + Add marketplace
      </Button>
      {!allowed && (
        <p id="add-market-why" className="text-xs text-text-muted">
          Your workspace policy only allows the official marketplace. Ask a workspace admin to change this.{' '}
          <Link to="/settings/$tab" params={{ tab: 'members' }} className="text-brand hover:underline">
            View workspace admins
          </Link>
        </p>
      )}
      {open && (
        <Dialog open onOpenChange={(o) => !o && !busy && close()}>
          <DialogContent data-testid="add-market-sheet" className="max-w-lg border-border bg-surface">
            <DialogHeader>
              <DialogTitle>Add marketplace</DialogTitle>
              <DialogDescription>Choose a source for add-ons. You will review it before adding.</DialogDescription>
            </DialogHeader>
            {picked ? (
              <div className="space-y-3 text-[13px]">
                <p className="font-medium">Approve this change</p>
                <p>
                  Marketplace <Inline>{repo(picked)}</Inline>
                </p>
                <p className="text-text-muted">Its add-ons will appear in Browse. Nothing is installed or turned on by adding a marketplace.</p>
                {error && <p role="alert" className="text-warning">{error}</p>}
                <div className="flex justify-end gap-2">
                  <Button variant="ghost" disabled={busy} onClick={() => setPicked(null)}>
                    Cancel
                  </Button>
                  <Button disabled={busy} onClick={() => void add(picked)}>
                    Approve
                  </Button>
                </div>
              </div>
            ) : (
              <div className="space-y-3">
                <ul aria-label="Known marketplaces" className="divide-y divide-border text-[13px]">
                  {markets.map((m) => (
                    <li key={m.url} className="flex items-center gap-3 py-2">
                      <div className="min-w-0 flex-1">
                        <p className="font-medium"><Inline>{m.from_marketplace?.title || m.from_marketplace?.name || repo(m.url)}</Inline></p>
                        <p className="text-text-muted">Repository: <Inline>{repo(m.url)}</Inline>{CLASS_WORD[m.class] ? ` · ${CLASS_WORD[m.class]}` : ''}</p>
                      </div>
                      <span className="text-text-faint">Already added</span>
                    </li>
                  ))}
                  {offered.map((o) => (
                    <li key={o.url} className="flex items-center gap-3 py-2">
                      <div className="min-w-0 flex-1">
                        <p className="font-medium">{o.class === 'dev' ? 'Dev test marketplace' : <Inline>{repo(o.url)}</Inline>}</p>
                        <p className="text-text-muted">Repository: <Inline>{repo(o.url)}</Inline>{CLASS_WORD[o.class] ? ` · ${CLASS_WORD[o.class]}` : ''}</p>
                      </div>
                      <Button size="sm" variant="secondary" onClick={() => setPicked(o.url)}>
                        Add
                      </Button>
                    </li>
                  ))}
                </ul>
                {anySource && (
                  <details data-testid="add-market-address">
                    <summary className="cursor-pointer text-[13px] text-text-muted">Add by address</summary>
                    <div className="mt-2 flex gap-2">
                      <Input aria-label="Marketplace address" placeholder="https://github.com/owner/repo" value={address} onChange={(e) => setAddress(e.target.value)} />
                      <Button size="sm" variant="secondary" disabled={!address.trim()} onClick={() => setPicked(address.trim())}>
                        Add
                      </Button>
                    </div>
                  </details>
                )}
              </div>
            )}
          </DialogContent>
        </Dialog>
      )}
    </div>
  )
}
