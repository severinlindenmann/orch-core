import { useState, type ReactNode } from 'react'
import { api } from '@/api/client'
import type { SettingsRequest } from '@/api/types'
import { SignPrompt, useSignedAction } from '@/components/sign/SignPrompt'

interface Pending {
  title: string
  covers: string[]
  req: SettingsRequest
  destructive?: boolean
}

/** Every settings change except rename is shown to the person and signed first (core's prompt). */
export function useSettingsSign(ws: string): { ask: (p: Pending) => void; prompt: ReactNode } {
  const signed = useSignedAction()
  const [pending, setPending] = useState<Pending | null>(null)
  const prompt = pending && (
    <SignPrompt
      title={pending.title}
      covers={pending.covers}
      destructive={pending.destructive}
      onClose={() => setPending(null)}
      onSign={() => {
        const p = pending
        setPending(null)
        void signed(p.title, () => api.postSettings(ws, p.req))
      }}
    />
  )
  return { ask: setPending, prompt }
}
