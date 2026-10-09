import { cn } from '@/lib/utils'
import { useEffect, useState } from 'react'
import type { Token } from './highlight'

const SHELL = new Set(['bash', 'sh', 'shell', 'zsh', 'console'])

export function CodeBlock({ language, text }: { language: string; text: string }) {
  const [lines, setLines] = useState<Token[][] | null>(null)
  useEffect(() => {
    let live = true
    import('./highlight')
      .then((m) => m.highlight(text, language))
      .then((l) => live && setLines(l))
      .catch(() => live && setLines(null))
    return () => {
      live = false
    }
  }, [text, language])
  return (
    // A shell command wraps so all of it is visible in a narrow rail; other code keeps its lines and scrolls.
    <pre className={cn('rounded-md border border-border bg-bg p-3 text-[12px] leading-5', SHELL.has(language) ? 'whitespace-pre-wrap break-all' : 'overflow-x-auto')} data-language={language}>
      <code>
        {lines
          ? lines.map((line, i) => (
              <div key={i}>
                {line.map((t, j) => (
                  <span key={j} style={t.color ? { color: t.color } : undefined}>
                    {t.content}
                  </span>
                ))}
                {line.length === 0 && '\n'}
              </div>
            ))
          : text}
      </code>
    </pre>
  )
}
