import { useRef, useState, useEffect } from 'react'
import { X } from 'lucide-react'
import type { Run } from '@/api/runs'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import type { TermView } from './termView'
import { transcript, transcriptName } from './transcript'

export function TermTools({ view, run, finding, onFinding }: {
  view: TermView | null; run: Run; finding: boolean; onFinding: (open: boolean) => void
}) {
  const [query, setQuery] = useState('')
  const [result, setResult] = useState('')
  const [feedback, setFeedback] = useState('')
  const input = useRef<HTMLInputElement>(null)
  useEffect(() => { if (finding) input.current?.focus() }, [finding])
  const copy = async () => {
    if (!view) return
    try {
      await navigator.clipboard.writeText(view.term.getSelection() || transcript(view.term))
      setFeedback('Copied')
    } catch { setFeedback('Could not copy') }
  }
  const download = () => {
    if (!view) return
    const url = URL.createObjectURL(new Blob([transcript(view.term)], { type: 'text/plain;charset=utf-8' }))
    const a = document.createElement('a')
    a.href = url
    a.download = transcriptName(run, new Date())
    try { a.click() } finally { setTimeout(() => URL.revokeObjectURL(url), 0) }
  }
  return <div className="shrink-0 border-b border-border bg-surface text-xs">
    <div className="flex flex-wrap items-center gap-2 px-2 py-1">
      <Button size="xs" variant="ghost" disabled={!view} onClick={() => onFinding(!finding)}>Find</Button>
      <Button size="xs" variant="ghost" disabled={!view} onClick={() => void copy()}>Copy</Button>
      <Button size="xs" variant="ghost" disabled={!view} onClick={download}>Download transcript</Button>
      <span role="status" className="text-text-muted">{feedback}</span>
    </div>
    {finding && <form className="flex flex-wrap items-center gap-2 px-2 py-1" onSubmit={(e) => {
      e.preventDefault()
      if (view && query) setResult(view.search.findNext(query, { caseSensitive: false }) ? 'Match found' : 'No matches')
    }}>
      <Input ref={input} aria-label="Find in terminal" value={query} onChange={(e) => setQuery(e.target.value)} className="h-6 min-w-0 flex-1 px-2 text-xs md:text-xs" />
      <Button type="submit" size="xs" variant="secondary" disabled={!view}>Find next</Button>
      <Button type="button" size="icon-xs" variant="ghost" aria-label="Close find" disabled={!view} onClick={() => { onFinding(false); view?.term.textarea?.focus() }}><X /></Button>
      <span role="status" className="text-text-muted">{result}</span>
    </form>}
  </div>
}
