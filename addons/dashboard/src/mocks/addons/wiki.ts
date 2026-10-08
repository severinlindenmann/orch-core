import { registerAddon } from './registry'

registerAddon({
  name: 'wiki',
  seed: () => ({}),
  actions: { open: () => ({ ok: true, message: 'Wiki editor arrives in a later iteration.' }) },
})
