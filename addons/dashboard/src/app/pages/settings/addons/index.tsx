import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { Fragment, useState } from 'react'
import { api } from '@/api/client'
import { addonActive, manifestFor, pendingUpdate, viewerActions } from '@/api/addons'
import type { AddonOpRequest, AddonPackage, InstalledAddon, Workspace } from '@/api/types'
import { useSignedAction, type SignedToast } from '@/components/sign/SignPrompt'
import { PIN_ADDON_EVENT } from '@/app/shell/pinEvent'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { AddonRow } from './AddonRow'
import { AddonSettingsPanel, requestSettingsFocus } from './AddonSettingsPanel'
import { Catalog } from './Catalog'
import { GrantDialog, type GrantAsk } from './GrantDialog'
import { toastApiError } from '@/app/toast'

/** The queries an install, enable, disable or uninstall can change. */
const ADDON_OP_KEYS = ['workspace-addons', 'workspaces', 'addons', 'addon-state', 'addon-decisions', 'today', 'ticket', 'board', 'agents']

/** Settings > Addons: installed addons with signed capability grants, and the catalog. */
export function AddonManager({ workspace, canEdit, settingsOf }: { workspace: Workspace; canEdit: boolean; settingsOf?: string }) {
  const navigate = useNavigate()
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
      // An addon turning on or off changes the lists of addons, every page's contributions, Today's cards and the board's lanes.
      await Promise.all(ADDON_OP_KEYS.map((k) => qc.invalidateQueries({ queryKey: [k] })))
    } catch (e) {
      toastApiError(e, 'Could not change the addon')
    }
  }
  /** The success toast of a grant that turned the addon on: Open its page, or Pin it to the sidebar. */
  const isOn = (pkg: AddonPackage): SignedToast => {
    const nav = pkg.contributions.find((c) => c.slot === 'nav')
    return {
      signedToast: true,
      message: `${pkg.title} is on`,
      ...(nav
        ? {
            action: { label: 'Open', onClick: () => void navigate({ to: `/addon/${pkg.name}/${nav.id}` } as never) },
            cancel: { label: 'Pin to sidebar', onClick: () => window.dispatchEvent(new CustomEvent(PIN_ADDON_EVENT, { detail: `${pkg.name}/${nav.id}` })) },
          }
        : {}),
    }
  }
  const sign = (a: GrantAsk) => {
    setAsk(null)
    // Send exactly what the prompt showed; the host refuses if the package changed in between.
    const update = a.kind === 'update' ? pendingUpdate(a.addon) : null
    const t = update ?? (a.kind === 'install' ? a.addon : a.addon.ws)
    const req: AddonOpRequest = { op: a.kind, version: t.version, package_sha256: t.package_sha256, capabilities: t.capabilities, viewer_actions: viewerActions(update ? { actions: update.actions ?? a.addon.actions } : manifestFor(a.addon, a.kind === 'install' ? a.addon.version : a.addon.ws.version)).map((x) => x.id), ...(a.kind === 'update' ? {} : { enable: true }) }
    void signed(a.kind === 'update' ? `Update ${a.addon.title}` : `Grant ${a.addon.title}`, async () => {
      await api.postAddonOp(ws, a.addon.name, req)
      return a.kind === 'update' ? `${a.addon.title} updated to ${t.version}` : isOn(a.addon)
    })
  }

  return (
    <div className="space-y-4">
      <div className="flex items-start gap-3">
        <div className="flex-1">
          <h2 tabIndex={-1} data-addons-heading className="text-base font-semibold outline-none">Addons</h2>
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
                <Fragment key={a.name}>
                <AddonRow
                  addon={a}
                  settingsOpen={settingsOf === a.name}
                  active={addonActive(workspace, a.name)}
                  canEdit={canEdit}
                  hasSettings={a.contributions.some((c) => c.slot === 'settings')}
                  actions={{
                    grant: () => setAsk({ kind: 'grant', addon: a }),
                    update: () => setAsk({ kind: 'update', addon: a }),
                    uninstall: () => setRemoving(a),
                    openSettings: () => {
                      requestSettingsFocus(a.name) // the list is drawn again by the route: Settings keeps the keyboard
                      void navigate(settingsOf === a.name ? { to: '/settings/$tab', params: { tab: 'addons' } } : { to: '/settings/addon/$name', params: { name: a.name } })
                    },
                    setEnabled: (on) => void run(a.name, { op: on ? 'enable' : 'disable' }),
                  }}
                />
                {settingsOf === a.name && (
                  <TableRow aria-label={`${a.title} settings`} className="hover:bg-transparent">
                    <TableCell colSpan={5} className="whitespace-normal bg-surface-2/30">
                      <AddonSettingsPanel name={a.name} workspace={workspace} canEdit={canEdit} />
                    </TableCell>
                  </TableRow>
                )}
                </Fragment>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      {/* A deep link to an addon that has no row (not installed): the panel says so, under the table. */}
      {settingsOf && !installed.isLoading && !installed.data?.some((a) => a.name === settingsOf) && <AddonSettingsPanel name={settingsOf} workspace={workspace} canEdit={canEdit} />}

      <Catalog
        ws={ws}
        canEdit={canEdit}
        open={browsing}
        onOpenChange={setBrowsing}
        onInstall={(pkg) => {
          setBrowsing(false)
          setAsk({ kind: 'install', addon: pkg })
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
