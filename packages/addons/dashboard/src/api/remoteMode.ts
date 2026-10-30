// Remote mode: this page runs inside a remote session (the workspace is reached from another computer through the
// remote client). The workspace refuses everything that is decided on its own computer (routegate REMOTE_DEVICE, D90:
// hub link and profile, phone pairing, add-on market, publishing, creating worktrees, setup). The UI must not offer
// what the workspace will refuse, so those screens and tabs are not drawn. Hiding is a courtesy: the workspace still
// refuses. Pure data and predicates; `features.ts` applies them.

/** How the workspace names a session opened through a stream (`GET /api/session` -> `login`). */
export const REMOTE_LOGIN = 'remote-device'

/** Features whose every action is local-only on the workspace's computer. */
export const REMOTE_HIDDEN_FEATURES: ReadonlySet<string> = new Set([
  'market', // add-on market: stage, add, uninstall are refused
  'remote', // who may reach this workspace remotely: codes and device removal are refused
  'publish', // shares: prepare, send and register are refused
  'worktrees', // creating a worktree is refused
])

/** Settings tabs that only change local things (devices and phone pairing, the hub profile, the hub link and company). */
export const REMOTE_HIDDEN_SETTINGS_TABS: ReadonlySet<string> = new Set(['devices', 'profile', 'hub', 'company'])

export const isRemoteLogin = (login: string | null | undefined): boolean => login === REMOTE_LOGIN
