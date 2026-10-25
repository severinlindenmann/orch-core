// While remote access is meant to be on but is not working (the session answer's `remote_problem`, loopback only), a
// one-line notice on every page links to Settings, Remote access: the person on the tunnel may never open Settings.
import { Link } from '@tanstack/react-router'
import { remoteInfo } from '@/api/features'

export function RemoteProblemBanner() {
  if (remoteInfo().problem === null) return null
  return (
    <div role="status" data-testid="remote-problem-banner" className="border-b border-warning/40 bg-warning-soft px-4 py-1.5 text-[13px] text-text">
      People cannot sign in from the web right now. You can still use orch here.{' '}
      <Link to="/settings/$tab" params={{ tab: 'remote' }} className="underline underline-offset-2">See details</Link>
    </div>
  )
}
