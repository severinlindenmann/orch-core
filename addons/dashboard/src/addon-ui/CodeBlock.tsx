import { useEffect, useState } from 'react'
import type { Token } from './highlight'

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
    <pre className="overflow-x-auto rounded-md border border-border bg-bg p-3 text-[12px] leading-5" data-language={language}>
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
