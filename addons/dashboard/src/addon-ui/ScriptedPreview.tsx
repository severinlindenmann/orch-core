import { CodeBlock } from './CodeBlock'

/** Core's words when agent HTML relied on scripts (round 2 #6): inert frames do not run them, so the source is shown. */
export const SCRIPTED_NOTICE = 'This preview used scripts, which orch no longer runs for agent HTML — showing the source.'

/**
 * What core draws instead of an inert frame for agent HTML that uses scripts: its notice, then the source (the frame
 * would be empty or misleading without them). `onPreview`, when given, offers the static preview anyway.
 */
export function ScriptedPreview({ html, onPreview }: { html: string; onPreview?: () => void }) {
  return (
    <div className="space-y-1.5">
      <p role="note" className="rounded-md border border-dashed border-border px-3 py-2 text-[12px] text-text-muted">
        {SCRIPTED_NOTICE}{' '}
        {onPreview && (
          <button type="button" onClick={onPreview} className="font-medium text-brand underline-offset-2 hover:underline">
            Show the static preview
          </button>
        )}
      </p>
      <CodeBlock language="html" text={html} />
    </div>
  )
}
