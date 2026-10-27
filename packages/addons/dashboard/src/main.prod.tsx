import './lib/zodJitless'
import { bootstrapSetup, isSetupPage } from './api/setup'
import { bootstrap, getPendingSwitch, landSwitch } from './api/bootstrap'
import { showBootFailure } from './api/bootFailure'
import './lib/cssom'
import './styles/tokens.css'

// Setup claims through the server; normal dashboard launch uses its separate bootstrap.
const setup = isSetupPage()
if (setup) {
  const session = bootstrapSetup()
  void session.catch(() => {})
  void import('./setup/Setup').then(({mount}) => mount(session)).catch(() => showBootFailure())
} else void bootstrap().catch(() => null).then(async () => {
  if (getPendingSwitch()) { // a hub link: this browser's key proves itself before the ticket is used, or we ask once
    const landing = await landSwitch()
    if (landing.kind === 'landed') {
      if (landing.to === null) return location.replace('/') // a sign-out ticket: this tab's session ended
      history.replaceState(null, '', landing.to)
    } else {
      const { showSwitchConfirm, showSwitchFailed } = await import('./api/switchConfirm')
      return landing.kind === 'confirm' ? showSwitchConfirm(landing.name) : showSwitchFailed()
    }
  }
  const { mount } = await import('./production/Shell')
  mount()
}).catch(() => showBootFailure()) // the app's own code did not load: never leave "Loading…" on screen for good
