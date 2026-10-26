// Remove an add-on from a marketplace (#1237 pick 2.1a): one plain sheet. If it is On the same sheet says it is turned
// off first and the button reads "Turn off and remove"; "Also delete its data" is an unticked box. The steps run in
// order under the sheet, each a normal signature (the review shows inside the sheet), then the package is removed.
import { useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { HandoffAction } from '@/api/handoff'
import { marketError, type InstalledAddon } from '@/api/market'
import { Inline } from '@/components/sign/visible'
import { SignPopover } from '@/components/sign/SignPopover'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'

type Step = { title: string; action: HandoffAction }

export function RemoveSheet({ ws, addon, onClose, onRemoved }: { ws: string; addon: InstalledAddon; onClose: () => void; onRemoved: (name: string) => void }) {
  const qc = useQueryClient()
  const on = addon.state === 'active'
  const [purge, setPurge] = useState(false)
  const [steps, setSteps] = useState<Step[] | null>(null)
  const [at, setAt] = useState(0)
  const [error, setError] = useState('')
  const [working, setWorking] = useState(false)
  const name = addon.name
  const signed = useRef(false)

  const finish = async () => {
    setWorking(true)
    try {
      await api.marketUninstall(ws, name)
      await Promise.all([qc.invalidateQueries({ queryKey: ['market', ws] }), qc.invalidateQueries({ queryKey: ['add-ons', ws] }), qc.invalidateQueries({ queryKey: ['workspaces'] })])
      onRemoved(name)
    } catch (e) {
      setError(marketError(e))
      setSteps(null)
    } finally {
      setWorking(false)
    }
  }
  const start = () => {
    const list: Step[] = [
      ...(on ? [{ title: `Turn off ${name}`, action: { action: 'addon_disable', name } as HandoffAction }] : []),
      ...(purge ? [{ title: `Delete the data of ${name}`, action: { action: 'addon_purge', name } as HandoffAction }] : []),
    ]
    setError('')
    if (!list.length) return void finish()
    signed.current = false
    setSteps(list)
    setAt(0)
  }
  const next = () => {
    signed.current = true
    if (steps && at + 1 < steps.length) setAt(at + 1)
    else void finish()
  }
  const current = steps?.[at]

  return (
    <Dialog open onOpenChange={(o) => !o && !working && !steps && onClose()}>
      <DialogContent data-testid="remove-sheet" className="max-w-md border-border bg-surface">
        <DialogHeader>
          <DialogTitle>
            Remove <Inline>{name}</Inline>?
          </DialogTitle>
          <DialogDescription>
            {on ? 'It is On: we will turn it off first. ' : ''}Agents lose its tools. Its saved settings and data stay on this computer unless you tick “Also delete its data”. To use it again, install it from Browse.
          </DialogDescription>
        </DialogHeader>
        <label className="flex items-center gap-2 text-[13px]">
          <input type="checkbox" checked={purge} disabled={!!steps || working} onChange={(e) => setPurge(e.target.checked)} />
          Also delete its data
        </label>
        {error && <p role="alert" className="text-[13px] text-warning">{error}</p>}
        {current && (
          <SignPopover
            key={`${at}-${current.title}`}
            title={current.title}
            ws={ws}
            action={current.action}
            autoStart
            autoSign
            inline
            onSettled={(phase) => {
              if (phase === 'done') next()
            }}
            onClose={() => {
              // closed without a signature: the removal stops here, nothing further happens
              if (!signed.current && !working) setSteps(null)
              signed.current = false
            }}
          />
        )}
        <DialogFooter>
          <Button variant="ghost" disabled={working} onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={!!steps || working} onClick={start}>
            {on ? 'Turn off and remove' : 'Remove'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
