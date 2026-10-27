// A hub-made (switch) session may read but not change anything. The server then answers `step_up_required`; the page
// asks the person to confirm on this computer (StepUpModal), and the refused action runs again once the session is a
// full one. One shared question at a time, however many requests were refused meanwhile.
export interface StepUpInfo { where: 'this_computer' | 'host'; message: string }

let pending: { promise: Promise<boolean>; settle: (ok: boolean) => void; info: StepUpInfo } | null = null
const listeners = new Set<(info: StepUpInfo | null) => void>()
const emit = () => listeners.forEach((fn) => fn(pending?.info ?? null))

export function onStepUp(fn: (info: StepUpInfo | null) => void) {
  listeners.add(fn)
  fn(pending?.info ?? null)
  return () => { listeners.delete(fn) }
}

export function requestStepUp(info: StepUpInfo): Promise<boolean> {
  if (pending) return pending.promise
  let settle!: (ok: boolean) => void
  const promise = new Promise<boolean>((resolve) => { settle = resolve })
  pending = { promise, settle, info }
  emit()
  return promise
}

export function settleStepUp(ok: boolean) {
  const p = pending
  pending = null
  emit()
  p?.settle(ok)
}

/** The structured refusal of a read-mostly session, or null for any other answer. */
export function stepUpOf(status: number, json: unknown): StepUpInfo | null {
  if (status !== 403 || typeof json !== 'object' || json === null) return null
  const err = (json as { error?: { code?: unknown; message?: unknown; step_up?: { where?: unknown } } }).error
  if (!err || err.code !== 'step_up_required') return null
  return {
    where: err.step_up?.where === 'host' ? 'host' : 'this_computer',
    message: typeof err.message === 'string' ? err.message.slice(0, 200) : "Confirm it's you on this computer to make this change.",
  }
}
