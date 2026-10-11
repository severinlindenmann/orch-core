import { bootstrap } from './api/bootstrap'
import './styles/tokens.css'

// Dynamic import intentionally follows the fragment scrub and launch exchange.
void bootstrap().catch(() => null).then(async () => {
  const { mount } = await import('./production/Shell')
  mount()
})
