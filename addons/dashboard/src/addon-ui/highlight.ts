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

const ALIASES: Record<string, string> = { sh: 'shellscript', bash: 'shellscript', shell: 'shellscript', ts: 'typescript', js: 'javascript', yml: 'yaml', py: 'python', md: 'markdown' }

/** Languages loaded on demand, each its own chunk (the first group above is loaded with the highlighter). */
const LAZY: Record<string, () => Promise<unknown>> = {
  html: () => import('@shikijs/langs/html'),
  css: () => import('@shikijs/langs/css'),
  markdown: () => import('@shikijs/langs/markdown'),
  toml: () => import('@shikijs/langs/toml'),
}

/**
 * The language of a file by its EXTENSION only (never by its content: no shebang or modeline sniffing). Null: no colours.
 * `log` and `txt` are plain text on purpose.
 */
const BY_EXT: Record<string, string> = {
  py: 'python', json: 'json', jsonl: 'json', md: 'markdown', markdown: 'markdown', toml: 'toml', yaml: 'yaml', yml: 'yaml',
  sh: 'shellscript', bash: 'shellscript', zsh: 'shellscript', ts: 'typescript', tsx: 'typescript', mts: 'typescript',
  js: 'javascript', jsx: 'javascript', mjs: 'javascript', cjs: 'javascript', css: 'css', html: 'html', htm: 'html',
  diff: 'diff', patch: 'diff', sql: 'sql',
}
export function langForFile(name: string): string | null {
  const ext = /\.([A-Za-z0-9]+)$/.exec(name)?.[1].toLowerCase()
  return (ext && BY_EXT[ext]) || null
}

/** The most lines that are coloured; a longer text is shown plain. Together with the 64 KiB text cap this bounds the work. */
export const MAX_HIGHLIGHT_LINES = 5000
const MAX_LINE = 1000
const LINE_LIMIT_MS = 20

/**
 * Token colour (the theme's fixed palette) to a class on a CSS variable (styles/tokens.css). A colour not listed draws in the
 * default text colour. Tokens are only ever drawn as text nodes with these classes: no HTML string, no inline style.
 */
const TOKEN_CLASS: Record<string, string> = {
  '#ff7b72': 'tok-keyword',
  '#79c0ff': 'tok-const',
  '#a5d6ff': 'tok-string',
  '#7ee787': 'tok-tag',
  '#d2a8ff': 'tok-func',
  '#ffa657': 'tok-name',
  '#8b949e': 'tok-comment',
  '#ffa198': 'tok-invalid',
}
export const tokenClass = (color?: string): string | undefined => (color ? TOKEN_CLASS[color.toLowerCase().slice(0, 7)] : undefined)

/**
 * The time every highlight may use in all (`tokenizeTimeLimit` is per LINE, so it alone cannot bound a file). The text is
 * tokenised a few lines at a time, carrying the grammar state across; once the budget is spent the caller gets null and
 * draws plain text. An agent-supplied file can therefore never keep the page busy for long.
 */
export const BUDGET_MS = 100
const CHUNK_LINES = 5

/** Returns lines of tokens, or null when the language is unknown, the text is too long, or the time budget ran out (render plain text then). */
export async function highlight(code: string, language: string, budgetMs: number = BUDGET_MS): Promise<Token[][] | null> {
  const h = await load()
  const lang = ALIASES[language] ?? language
  const all = code.split('\n')
  if (all.length > MAX_HIGHLIGHT_LINES) return null
  if (!h.getLoadedLanguages().includes(lang) && LAZY[lang]) await h.loadLanguage(LAZY[lang]() as never)
  if (!h.getLoadedLanguages().includes(lang)) return null
  const started = performance.now()
  const out: Token[][] = []
  let state: ReturnType<typeof h.getLastGrammarState> | undefined
  for (let i = 0; i < all.length; i += CHUNK_LINES) {
    if (performance.now() - started > budgetMs) return null
    // The token API only (never an HTML-producing call): the page draws the tokens as React text.
    const part = h.codeToTokensBase(all.slice(i, i + CHUNK_LINES).join('\n'), { lang, theme: THEME, tokenizeMaxLineLength: MAX_LINE, tokenizeTimeLimit: LINE_LIMIT_MS, grammarState: state })
    for (const line of part) out.push(line.map((t) => ({ content: t.content, color: t.color })))
    state = h.getLastGrammarState(part as never)
  }
  return performance.now() - started > budgetMs * 2 ? null : out
}
