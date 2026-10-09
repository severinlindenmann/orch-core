import { Mic, Square } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { SAMPLE_TRANSCRIPTS } from './quickRules'

/** Longest simulated recording; it stops by itself after this. */
export const DICTATE_MAX_S = 60
const BARS = 12

const reducedMotion = () => typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
const clock = (s: number) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`

let nextSample = 0

/**
 * Dictation, simulated: no microphone is opened (no getUserMedia, no MediaRecorder). Recording shows a timer and a
 * level meter; Stop hands over the next sample transcription. The meter holds still under prefers-reduced-motion.
 */
export function Dictate({ onTranscript, disabled }: { onTranscript: (text: string) => void; disabled?: boolean }) {
  const [recording, setRecording] = useState(false)
  const [seconds, setSeconds] = useState(0)
  const [levels, setLevels] = useState<number[]>(() => Array(BARS).fill(0.2))
  const still = useRef(reducedMotion())
  const stopRef = useRef<() => void>(() => {})

  const stop = (keep: boolean) => {
    setRecording(false)
    setSeconds(0)
    if (keep) onTranscript(SAMPLE_TRANSCRIPTS[nextSample++ % SAMPLE_TRANSCRIPTS.length])
  }
  stopRef.current = () => stop(true)

  useEffect(() => {
    if (!recording) return
    const started = Date.now()
    const tick = window.setInterval(() => {
      const s = Math.floor((Date.now() - started) / 1000)
      if (s >= DICTATE_MAX_S) stopRef.current()
      else setSeconds(s)
    }, 250)
    const meter = still.current ? null : window.setInterval(() => setLevels((prev) => prev.map((_, i) => 0.15 + Math.abs(Math.sin(Date.now() / 180 + i * 1.7)) * (0.35 + Math.random() * 0.5))), 120)
    return () => {
      window.clearInterval(tick)
      if (meter !== null) window.clearInterval(meter)
    }
  }, [recording])

  if (!recording)
    return (
      <Button type="button" variant="outline" size="icon" className="size-9 shrink-0" aria-label="Dictate (simulated)" title="Dictate (simulated — no audio leaves your browser)" disabled={disabled} onClick={() => setRecording(true)}>
        <Mic />
      </Button>
    )

  return (
    <div role="group" aria-label="Dictation" className="order-last flex h-9 w-full items-center gap-2 rounded-md border border-border bg-surface-2 pl-2.5 pr-1">
      <span aria-hidden className="size-2 rounded-full bg-danger motion-safe:animate-pulse" />
      <span role="status" className="sr-only">
        Recording (simulated)
      </span>
      <span className="font-mono text-[12px] tabular-nums text-text" aria-label={`Recording, ${seconds} seconds`}>
        {clock(seconds)}
      </span>
      <span aria-hidden data-testid="level-meter" data-still={still.current || undefined} className="flex h-5 items-center gap-[2px]">
        {levels.map((l, i) => (
          <span key={i} className="w-[3px] rounded-full bg-text-muted" style={{ height: `${Math.round(l * 100)}%` }} />
        ))}
      </span>
      <span className="flex-1 truncate text-[12px] text-text-muted">Simulated — no audio leaves your browser</span>
      <Button type="button" variant="ghost" size="sm" className="h-7 px-2" onClick={() => stop(false)}>
        Cancel
      </Button>
      <Button type="button" size="sm" className="h-7 px-2" onClick={() => stop(true)} autoFocus>
        <Square className="size-3" />
        Stop
      </Button>
    </div>
  )
}
