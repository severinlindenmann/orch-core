import './lib/zodJitless'
import { bootstrapSetup, isSetupPage } from './api/setup'
import { bootstrap } from './api/bootstrap'
import './lib/cssom'
import './styles/tokens.css'

// Setup claims through the server; normal dashboard launch uses its separate bootstrap.
const setup = isSetupPage()
if (setup) {
  const session = bootstrapSetup()
  void session.catch(() => {})
  void import('./setup/Setup').then(({mount}) => mount(session))
} else void bootstrap().catch(() => null).then(async () => {
  const { mount } = await import('./production/Shell')
  mount()
})
