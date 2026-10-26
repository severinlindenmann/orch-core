export function Conversation({ ticket }: { ticket?: string }) {
  return <section data-testid="focus-conversation" aria-label={ticket ? 'Ticket conversation' : 'Scratch conversation'} className="min-w-0 flex-1 p-5 text-sm text-text-muted">Conversation coming in F2.</section>
}
