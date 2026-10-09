import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '@/api/client'
import { addonActive, manifestFor, pendingUpdate, viewerActions } from '@/api/addons'
import type { AddonOpRequest, InstalledAddon, Workspace } from '@/api/types'
import { useSignedAction } from '@/components/sign/SignPrompt'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { AddonRow } from './AddonRow'
import { Catalog } from './Catalog'
import { GrantDialog, type GrantAsk } from './GrantDialog'
import { toastApiError } from '@/app/toast'

/** Settings > Addons: installed addons with signed capability grants, and the catalog. */
export function AddonManager({ workspace, canEdit }: { workspace: Workspace; canEdit: boolean }) {
  const ws = workspace.id
  const qc = useQueryClient()
  const signed = useSignedAction()
  const installed = useQuery({ queryKey: ['workspace-addons', ws], queryFn: () => api.getWorkspaceAddons(ws) })
  const [browsing, setBrowsing] = useState(false)
  const [ask, setAsk] = useState<GrantAsk | null>(null)
  const [removing, setRemoving] = useState<InstalledAddon | null>(null)

  /** Unsigned ops (install, enable, disable, uninstall): run, refetch, toast the refusal. */
  const run = async (name: string, req: AddonOpRequest) => {
    try {
      await api.postAddonOp(ws, name, req)
      await qc.invalidateQueries()
    } catch (e) {
      toastApiError(e, 'Could not change the addon')
    }
  }
  const sign = (a: GrantAsk) => {
    setAsk(null)
    // Send exactly what the prompt showed; the host refuses if the package changed in between.
    const update = a.kind === 'update' ? pendingUpdate(a.addon) : null
    const t = update ?? a.addon.ws
    const req: AddonOpRequest = { op: a.kind, version: t.version, package_sha256: t.package_sha256, capabilities: t.capabilities, viewer_actions: viewerActions(update ? { actions: update.actions ?? a.addon.actions } : manifestFor(a.addon, a.addon.ws.version)).map((x) => x.id) }
    void signed(a.kind === 'update' ? `Update ${a.addon.title}` : `Grant ${a.addon.title}`, () => api.postAddonOp(ws, a.addon.name, req))
  }

  return (
    <div className="space-y-4">
      <div className="flex items-start gap-3">
        <div className="flex-1">
          <h2 className="text-base font-semibold">Addons</h2>
          <p className="mt-1 text-[13px] text-text-muted">The owner grants what each addon may do. Every update needs a new grant; agents never enable addons.</p>
        </div>
        <Button variant="outline" onClick={() => setBrowsing(true)}>
          Browse addons
        </Button>
      </div>

      {installed.isLoading ? (
        <Skeleton className="h-40 w-full" />
      ) : (
        <div className="rounded-lg border border-border bg-surface">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Addon</TableHead>
                <TableHead>Capabilities</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>On</TableHead>
                <TableHead>
                  <span className="sr-only">Actions</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {(installed.data ?? []).map((a) => (
                <AddonRow
                  key={a.name}
                  addon={a}
                  active={addonActive(workspace, a.name)}
                  canEdit={canEdit}
                  hasSettings={a.contributions.some((c) => c.slot === 'settings')}
                  actions={{
                    grant: () => setAsk({ kind: 'grant', addon: a }),
                    update: () => setAsk({ kind: 'update', addon: a }),
                    uninstall: () => setRemoving(a),
                    setEnabled: (on) => void run(a.name, { op: on ? 'enable' : 'disable' }),
                  }}
                />
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <Catalog
        ws={ws}
        canEdit={canEdit}
        open={browsing}
        onOpenChange={setBrowsing}
        onInstall={(name) => {
          setBrowsing(false)
          void run(name, { op: 'install' })
        }}
      />
      {ask && <GrantDialog ask={ask} onSign={() => sign(ask)} onClose={() => setAsk(null)} />}
      {removing && (
        <Dialog open onOpenChange={(o) => !o && setRemoving(null)}>
          <DialogContent className="max-w-md border-border bg-surface">
            <DialogHeader>
              <DialogTitle>Uninstall {removing.title}?</DialogTitle>
              <DialogDescription>Its ticket data stays where it is and is shown inactive. Installing it again brings it back, with a new grant.</DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="ghost" onClick={() => setRemoving(null)}>
                Cancel
              </Button>
              <Button
                variant="destructive"
                onClick={() => {
                  const name = removing.name
                  setRemoving(null)
                  void run(name, { op: 'uninstall' })
                }}
              >
                Uninstall
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </div>
  )
}
