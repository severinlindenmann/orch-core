import { Panel } from './shared'
export function NotTold(_: { ws: string }) {
  return <Panel title="You left the hub, but the hub does not know yet" tone="border-warning/40 bg-warning-soft">
    <p>The workspace keeps trying to tell the hub whenever it is reachable.</p>
    <p>One click is not ready for stopping these attempts.</p>
  </Panel>
}
