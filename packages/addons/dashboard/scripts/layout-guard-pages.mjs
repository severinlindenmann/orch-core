// The real-host pages the layout guard draws from fixtures (`/__layout/<scenario>`, src/dev/layoutPreview.tsx). The guard enters
// that block when `--only` matches any of these names, so `--only terminal` finds its pages.
export const REAL_HOST_PAGES = [
  'real-host-quick', 'real-host-quick-many', 'real-host-quick-normal-ticket', 'real-host-quick-add',
  'real-host-records', 'real-host-records-hidden-url', 'real-host-records-first-commit', 'real-host-records-push-failed',
  'real-host-records-push-mismatch', 'real-host-records-push', 'real-host-records-push-confirm',
  'real-host-runner-on', 'real-host-runner-off', 'real-host-addons', 'real-host-agent-model', 'real-host-addons-publish', 'real-host-addons-publish-open', 'real-host-addons-publish-unset',
  'real-host-terminal-dock', 'real-host-terminal-preview', 'real-host-terminal-readonly', 'real-host-terminal-take',
  'real-host-terminal-exited', 'real-host-terminal-stop',
  'real-host-readers-devices', 'real-host-convenience-refusals',
  'real-host-signing-on', 'real-host-signing-off', 'real-host-signing-terminal-on', 'real-host-signing-unsafe', 'real-host-signing-unknown',
  'real-host-dev-badge', 'real-host-dev-badge-long', 'real-host-dev-alarms', 'real-host-dev-refused', 'real-host-signing-dev-host',
  'real-host-drop-unavailable', 'real-host-drop-list', 'real-host-drop-long-names', 'real-host-drop-upload-progress',
  'real-host-drop-errors', 'real-host-drop-versions', 'real-host-drop-phone', 'real-host-drop-delete',
]

/** Does `--only` (a RegExp or null) select at least one real-host page? */
export const selectsRealHost = (only) => !only || REAL_HOST_PAGES.some((n) => only.test(n))

// Mock Settings > Hub pages. Keep the scenario in the URL so each visit resets the hub state.
export const SETTINGS_HUB_PAGES = ['standalone', 'selective', 'full', 'offline'].map((scenario) => [
  `settings-hub-${scenario}`, `/settings/hub?hub=${scenario}`,
])
