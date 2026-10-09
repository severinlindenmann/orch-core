// Lazy shiki highlighter: fine-grained bundle + JS regex engine (no wasm), loaded only when a code node renders.
import type { HighlighterCore } from 'shiki/core'

export interface Token {
  content: string
  color?: string
}

const THEME = 'github-dark-default'
let pending: Promise<HighlighterCore> | null = null

function load() {
  pending ??= (async () => {
    const [{ createHighlighterCore }, { createJavaScriptRegexEngine }] = await Promise.all([
      import('shiki/core'),
      import('shiki/engine/javascript'),
    ])
    return createHighlighterCore({
      themes: [import('@shikijs/themes/github-dark-default')],
      langs: [
        import('@shikijs/langs/sql'),
        import('@shikijs/langs/python'),
        import('@shikijs/langs/typescript'),
        import('@shikijs/langs/javascript'),
        import('@shikijs/langs/json'),
        import('@shikijs/langs/yaml'),
        import('@shikijs/langs/shellscript'),
        import('@shikijs/langs/diff'),
      ],
      engine: createJavaScriptRegexEngine(),
    })
  })()
  return pending
}

const ALIASES: Record<string, string> = { sh: 'shellscript', bash: 'shellscript', shell: 'shellscript', ts: 'typescript', js: 'javascript', yml: 'yaml', py: 'python' }

/** Returns lines of tokens, or null when the language is unknown (render plain text then). */
export async function highlight(code: string, language: string): Promise<Token[][] | null> {
  const h = await load()
  const lang = ALIASES[language] ?? language
  if (!h.getLoadedLanguages().includes(lang)) return null
  return h.codeToTokensBase(code, { lang, theme: THEME }).map((line) => line.map((t) => ({ content: t.content, color: t.color })))
}
