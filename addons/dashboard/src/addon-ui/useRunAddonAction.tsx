// The one way the dashboard runs an addon action (addon nodes, board lanes, the command palette). It applies the
// manifest of the installed version: who may run it (minRole), core's own dialogs (`confirm: 'sign'` and
// `'spawn_agent'`), navigation (`kind: 'navigation'`: quiet), reserved-key stripping, toasts, refetching and
// opening a result url. The host checks it all again.
import { useState, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { manifestFor } from '@/api/addons'
import { api } from '@/api/client'
import { atLeast } from '@/api/permissions'
import { ApiError, type ActionMeta, type AddonActionResult, type AddonDecision, type Role } from '@/api/types'
import { toastApiError } from '@/app/toast'
import { useRole } from '@/app/useRole'
import { useWorkspace } from '@/app/workspace'
import { useSignedAction } from '@/components/sign/SignPrompt'
import { openResultUrl, withoutReservedKeys } from './actionRuntime'
import { DecisionSignPrompt, decisionBody } from './DecisionSignPrompt'
import { DestructiveConfirm } from './DestructiveConfirm'
import { SecretDialog } from './SecretDialog'
import { SignConfirm, signTitle } from './SignConfirm'
import { SpawnConfirm, type ConfirmedLaunch } from './SpawnConfirm'
import { addonStateKey, useAddons } from './slots'

interface Pending {
  addon: string
  action: string
  extra?: Record<string, unknown>
  /** What the action is about, in the surface's words (the row's title): core's signing prompt shows it instead of raw ids. */
  subject?: string
}

export interface RunAddonAction {
  /** Runs `action` of `addon`; opens core's dialog first when the manifest says so. `subject`: what it is about, as the row names it. */
  run: (addon: string, action: string, extra?: Record<string, unknown>, subject?: string) => void
  /** May the viewer run it (role in this workspace meets the installed manifest's minRole)? */
  allowed: (addon: string, action: string) => boolean
  /** The manifest entry of an action (installed version). */
  meta: (addon: string, action: string) => ActionMeta | undefined
  pending: boolean
  /** The last refusal of this hook's own action (when `inlineErrors`): the surface shows it under the trigger until the next success. */
  error: ActionError | null
  dismissError: () => void
  /** Core's confirm, signing or show-once dialog; render it next to the trigger. */
  dialog: ReactNode
}

export interface ActionError {
  message: string
  hint?: string
}

export interface RunOptions {
  /**
   * The surface shows a refusal itself (an addon node: a persistent alert under the trigger), so the hook does not
   * toast it. Surfaces without room for that (the palette, lanes, the ticket header) leave this off and get an error toast.
   */
  inlineErrors?: boolean
}

/** Why an action is not allowed for `role`, in plain words (null when it is). */
export function roleReason(role: Role | undefined, min: Role): string | null {
  if (atLeast(role, min)) return null
  if (!role) return 'You are not a member of this workspace.'
  if (min === 'owner') return 'Only owners can do this.'
  if (min === 'maintainer') return 'Only owners and maintainers can do this.'
  return 'Viewers cannot do this.'
}

/** `ticket`: the ticket in core's render context (sent as `ticket`); addon args can never set it. */
export function useRunAddonAction(ticket?: string, opts: RunOptions = {}): RunAddonAction {
  const qc = useQueryClient()
  const { workspace } = useWorkspace()
  const role = useRole()
  const { data: packages } = useAddons()
  const signed = useSignedAction()
  const [confirming, setConfirming] = useState<Pending | null>(null)
  const [signing, setSigning] = useState<Pending | null>(null)
  const [signPending, setSignPending] = useState(false)
  const [error, setError] = useState<ActionError | null>(null)
  const [secret, setSecret] = useState<{ addon: string; secret: NonNullable<AddonActionResult['secret']> } | null>(null)
  const [destroying, setDestroying] = useState<Pending | null>(null)

  const meta = (addon: string, action: string): ActionMeta | undefined => {
    const pkg = packages?.find((a) => a.name === addon)
    // Same manifest the server enforces: the installed version's, so an update that removed a viewer action disables it here.
    return pkg ? manifestFor(pkg, workspace?.addons[addon]?.version ?? pkg.version).actions?.[action] : undefined
  }
  const allowed = (addon: string, action: string) => !!workspace && atLeast(role, meta(addon, action)?.minRole ?? 'member')
  /** Undo only for the pair the manifest declares, and only when the target is a plain action this viewer may run. */
  const undoAllowed = (addon: string, action: string, target: string) => {
    if (meta(addon, action)?.undo !== target) return false
    const t = meta(addon, target)
    return !t?.confirm && !t?.decision && t?.kind !== 'navigation' && allowed(addon, target)
  }
  const titleOf = (addon: string) => packages?.find((p) => p.name === addon)?.title ?? addon
  const body = (extra?: Record<string, unknown>) => ({ ...withoutReservedKeys(extra), ...(ticket ? { ticket } : {}) })

  const m = useMutation({
    mutationFn: ({ addon, action, extra, confirmed }: Pending & { confirmed?: ConfirmedLaunch }) => {
      if (!workspace) throw new Error('No workspace')
      // After core's dialog: the ticket and choice core validated and showed, never the addon's own args for them.
      const core = confirmed ? { confirmed: true, ticket: confirmed.ticket, launch: { mode: confirmed.mode, harness: confirmed.harness, where: confirmed.where } } : {}
      return api.runAddonAction(workspace.id, addon, action, { ...body(extra), ...core })
    },
    onMutate: () => setError(null),
    onSuccess: (res, { addon, action }) => {
      openResultUrl(res)
      // Navigation moves only this viewer's view: no toast, and only this addon's state is read again.
      if (meta(addon, action)?.kind === 'navigation') {
        void qc.invalidateQueries({ queryKey: addonStateKey(workspace?.id, addon) })
        return
      }
      // A value shown once never rides in a toast: core's modal holds it until the person says they saved it.
      if (res.secret) setSecret({ addon, secret: res.secret })
      else if (res.undo && undoAllowed(addon, action, res.undo.action)) {
        const undo = res.undo
        toast.success(res.message, { action: { label: 'Undo', onClick: () => run(addon, undo.action, undo.args) } })
      } else toast.success(res.message)
      void qc.invalidateQueries({ queryKey: ['addon-state'] })
      void qc.invalidateQueries({ queryKey: ['ticket'] })
      void qc.invalidateQueries({ queryKey: ['today'] })
      if (res.changed) void qc.invalidateQueries()
    },
    // A refusal can still have been recorded in the addon's state (e.g. a rejected push): read it again.
    onError: (err, { addon }) => {
      if (opts.inlineErrors) setError(err instanceof ApiError ? { message: err.message, hint: err.hint } : { message: 'That did not work.' })
      else toastApiError(err, 'Action failed')
      void qc.invalidateQueries({ queryKey: addonStateKey(workspace?.id, addon) })
    },
  })

  // A decision action is never posted from an addon surface as it stands: core looks the decision up itself (the
  // addon's args only say which one), shows its own prompt with core's facts, and posts core's body after signing.
  const [deciding, setDeciding] = useState<{ addon: string; action: string; d: AddonDecision; option: AddonDecision['options'][number] } | null>(null)
  const openDecision = async (addon: string, action: string, extra?: Record<string, unknown>) => {
    if (!workspace) return
    try {
      const open = await qc.fetchQuery({ queryKey: ['addon-decisions', workspace.id], queryFn: () => api.getAddonDecisions(workspace.id) })
      const d = open.find((x) => x.addon === addon && x.action === action && x.id === extra?.id)
      const option = d?.options.find((o) => o.key === extra?.option)
      if (!d) return void toast.error('That decision is closed.')
      if (!option) return void toast.error(`Choose ${d.options.map((o) => o.label).join(', ')}.`)
      setDeciding({ addon, action, d, option })
    } catch (e) {
      toastApiError(e, 'Could not open the decision')
    }
  }

  const run = (addon: string, action: string, extra?: Record<string, unknown>, subject?: string) => {
    const m0 = meta(addon, action)
    const confirm = m0?.confirm
    if (m0?.decision) void openDecision(addon, action, extra)
    else if (confirm === 'spawn_agent') setConfirming({ addon, action, extra })
    else if (confirm === 'sign') setSigning({ addon, action, extra, subject })
    else if (confirm === 'destructive') setDestroying({ addon, action, extra })
    else m.mutate({ addon, action, extra })
  }

  // `confirm: 'sign'`: core's signing prompt first; only after Touch ID is the action posted, with core's `confirmed` flag.
  const signDialog = signing && workspace && (
    <SignConfirm
      addon={signing.addon}
      addonTitle={titleOf(signing.addon)}
      action={signing.action}
      workspace={{ prefix: workspace.prefix, name: workspace.name }}
      label={meta(signing.addon, signing.action)?.label}
      args={withoutReservedKeys(signing.extra)}
      subject={signing.subject}
      onClose={() => setSigning(null)}
      onSign={() => {
        const s = signing
        setSigning(null)
        setSignPending(true)
        void signed(signTitle(s.action, titleOf(s.addon)), async () => {
          const res = await api.runAddonAction(workspace.id, s.addon, s.action, { ...body(s.extra), confirmed: true })
          openResultUrl(res)
          return res.message
        }).finally(() => setSignPending(false))
      }}
    />
  )
  const decisionDialog = deciding && workspace && (
    <DecisionSignPrompt
      d={deciding.d}
      option={deciding.option}
      workspacePrefix={workspace.prefix}
      onClose={() => setDeciding(null)}
      onSign={() => {
        const x = deciding
        setDeciding(null)
        setSignPending(true)
        void signed(`Decide: ${x.d.title}`, async () => {
          const res = await api.runAddonAction(workspace.id, x.addon, x.action, decisionBody(x.d, x.option.key))
          return res.message
        }).finally(() => setSignPending(false))
      }}
    />
  )
  const destroyDialog = destroying && (
    <DestructiveConfirm
      label={meta(destroying.addon, destroying.action)?.confirmLabel ?? meta(destroying.addon, destroying.action)?.label ?? 'Confirm'}
      text={meta(destroying.addon, destroying.action)?.confirmText}
      onClose={() => setDestroying(null)}
      onConfirm={() => {
        const d = destroying
        setDestroying(null)
        m.mutate(d)
      }}
    />
  )
  const secretDialog = secret && <SecretDialog addon={secret.addon} secret={secret.secret} onDone={() => setSecret(null)} />
  const dialog = secretDialog || decisionDialog || signDialog || destroyDialog || (confirming && (
    <SpawnConfirm addon={confirming.addon} ticketKey={ticket} onClose={() => setConfirming(null)} onStart={(launch) => m.mutate({ ...confirming, confirmed: launch })} />
  ))
  return { run, allowed, meta, pending: m.isPending || signPending, error, dismissError: () => setError(null), dialog }
}
