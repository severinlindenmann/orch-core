import { Link } from '@tanstack/react-router'
import { hasFeature } from '@/api/features'
import { useWorkspace } from '@/app/workspace'
import { Button } from '@/components/ui/button'

export function FocusButton({ ticket }: { ticket: string }) {
  const { workspace } = useWorkspace()
  if (!workspace || !hasFeature('focus', workspace.id)) return null
  return <Button asChild variant="outline" size="sm" data-testid="focus-button"><Link to="/focus/$ticket" params={{ ticket }}>Focus</Link></Button>
}
