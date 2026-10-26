import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { blockedText } from '@/api/agents'
import { delegateApi, type DelegateArgs } from '@/api/focus'
import { hasFeature, isRealHost } from '@/api/features'
import { modelLabel, routingQueries } from '@/api/modelRouting'
import { newIdem, type Run } from '@/api/runs'
import { ApiError } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'

const field = 'w-full rounded-md border border-border bg-surface p-2 text-sm text-text'

/** The F5 form: hand one task to a second agent that works beside the main session (same folder). */
export function DelegateSheet({ ws, ticket, parent, used, max, onClose }: { ws: string; ticket: string; parent: Run; used: number; max: number | null; onClose: () => void }) {
  const client = useQueryClient()
  const [idem] = useState(() => newIdem())
  const [title, setTitle] = useState('')
  const [brief, setBrief] = useState('')
  const [expected, setExpected] = useState('')
  const [harness, setHarness] = useState<'claude-code' | 'codex'>(parent.harness === 'codex' ? 'codex' : 'claude-code')
  const [model, setModel] = useState('')
  const [error, setError] = useState('')
  const routing = hasFeature('model_routing', ws)
  const choices = useQuery({ ...routingQueries.choices(ws), enabled: routing })
  const models = choices.data?.harnesses?.find((h) => h.id === harness)?.models ?? []
  const full = max !== null && used >= max
  const args: DelegateArgs = {
    ticket, harness, mode: 'task', parent_run: parent.run, label: title.trim(), worktree: true,
    ...(brief.trim() ? { brief: brief.trim() } : {}), ...(expected.trim() ? { expected: expected.trim() } : {}), ...(model ? { model } : {}),
  }
  const send = useMutation({
    mutationFn: async () => {
      if (!isRealHost()) throw new Error('Delegating a task is unavailable in this demo.')
      const pre = await delegateApi.preview(ws, args)
      if (pre.blocked) throw new Error(blockedText(pre.blocked) ?? pre.blocked.message ?? 'The task could not be started.')
      if (!pre.spec) throw new Error('The task could not be previewed.')
      return delegateApi.start(ws, args, pre.spec, idem)
    },
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ['focus', 'delegations', ws, ticket] })
      await client.invalidateQueries({ queryKey: ['runs', ws] })
      onClose()
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : 'The task could not be started.'),
  })
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg" data-testid="focus-delegate-sheet">
        <DialogHeader>
          <DialogTitle>Delegate a task</DialogTitle>
          <DialogDescription>A second agent takes this task and works beside the main session.</DialogDescription>
        </DialogHeader>
        <form className="space-y-3 text-sm" onSubmit={(e) => { e.preventDefault(); setError(''); if (title.trim() && !full) send.mutate() }}>
          <div className="space-y-1"><Label htmlFor="delegate-title">Task title</Label><Input id="delegate-title" value={title} maxLength={40} autoComplete="off" onChange={(e) => setTitle(e.target.value)} /></div>
          <div className="space-y-1"><Label htmlFor="delegate-brief">What to do</Label><Textarea id="delegate-brief" className={field} rows={4} maxLength={4000} value={brief} onChange={(e) => setBrief(e.target.value)} /></div>
          <div className="space-y-1"><Label htmlFor="delegate-expected">Expected output</Label><Input id="delegate-expected" value={expected} maxLength={400} autoComplete="off" onChange={(e) => setExpected(e.target.value)} /></div>
          <p data-testid="delegate-scope" className="text-xs text-text-muted">It works in the same folder as the main session, so both can change the same files. Changes show who made them.</p>
          <details className="text-xs">
            <summary className="cursor-pointer text-text-muted">More options</summary>
            <div className="mt-2 grid gap-2">
              <Label htmlFor="delegate-harness">Agent</Label>
              <select id="delegate-harness" className={field} value={harness} onChange={(e) => { setHarness(e.target.value as 'claude-code' | 'codex'); setModel('') }}>
                <option value="claude-code">Claude Code</option><option value="codex">Codex</option>
              </select>
              {models.length > 0 && <><Label htmlFor="delegate-model">Model</Label>
                <select id="delegate-model" className={field} value={model} onChange={(e) => setModel(e.target.value)}>
                  <option value="">Chosen by the agent app</option>
                  {models.map((m) => <option key={m.id} value={m.id}>{modelLabel(choices.data, harness, m.id)}</option>)}
                </select></>}
            </div>
          </details>
          {max !== null && <p data-testid="delegate-budget" className="text-xs text-text-muted">{used} of {max} delegated tasks used. The limit applies at the next start of the main session.</p>}
          {error && <p role="alert" className="text-xs">{error}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" size="sm" onClick={onClose}>Cancel</Button>
            <Button type="submit" size="sm" disabled={!title.trim() || full || send.isPending}>{send.isPending ? 'Starting...' : 'Delegate'}</Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  )
}
