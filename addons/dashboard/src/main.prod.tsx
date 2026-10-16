import { bootstrapSetup } from './api/setup'
import { bootstrap } from './api/bootstrap'
import './styles/tokens.css'

// Dynamic import intentionally follows the fragment scrub and launch exchange.
const setup = location.pathname === '/setup' || new URLSearchParams(location.hash.slice(1)).get('to') === '/setup'
if (setup) {
  const session = bootstrapSetup()
  void session.catch(() => {})
  void import('./setup/Setup').then(({mount}) => mount(session))
} else void bootstrap().catch(() => null).then(async () => {
  const { mount } = await import('./production/Shell')
  mount()
})
