import { useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import { marketError, type InstalledAddon } from '@/api/market'
import { SignPopover } from '@/components/sign/SignPopover'
import { Button } from '@/components/ui/button'

/** Removal keeps saved data. An active add-on's signature is the only confirmation. */
export function RemoveSheet({ ws, addon, onClose, onRemoved }: { ws: string; addon: InstalledAddon; onClose: () => void; onRemoved: (name: string) => void }) {
  const qc = useQueryClient()
  const [error, setError] = useState('')
  const working = useRef(false)
  const finish = async () => {
    if (working.current) return
    working.current = true
    try {
      await api.marketUninstall(ws, addon.name)
      await Promise.all([qc.invalidateQueries({ queryKey: ['market', ws] }), qc.invalidateQueries({ queryKey: ['add-ons', ws] }), qc.invalidateQueries({ queryKey: ['workspaces'] })])
      onRemoved(addon.name)
    } catch (e) { setError(marketError(e)) }
    finally { working.current = false }
  }
  return <div data-testid="remove-sheet">
    {error ? <><p role="alert">{error}</p><Button onClick={() => void finish()}>Try again</Button></> : <SignPopover ws={ws} title={`Turn off and remove ${addon.name}`} action={{ action: 'addon_disable', name: addon.name }} inline onClose={onClose} onSettled={phase => { if (phase === 'done') void finish() }} unsigned={<p>Saved settings and data stay on this computer.</p>} />}
  </div>
}
