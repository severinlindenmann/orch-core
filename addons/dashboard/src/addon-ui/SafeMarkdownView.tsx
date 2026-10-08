import ReactMarkdown from 'react-markdown'
import rehypeSanitize from 'rehype-sanitize'
import remarkGfm from 'remark-gfm'

const isHttp = (href?: string) => !!href && /^https?:\/\//i.test(href)

/**
 * Markdown from an addon. Raw HTML is never interpreted (no rehype-raw), the output is sanitized,
 * links are only http(s) and open in a new tab, and images are never loaded (an alt-text placeholder instead).
 */
export function SafeMarkdownView({ text }: { text: string }) {
  return (
    <div className="addon-md text-[13px] leading-relaxed text-text [&_blockquote]:border-l-2 [&_blockquote]:border-border-strong [&_blockquote]:pl-3 [&_blockquote]:text-text-muted [&_code]:rounded [&_code]:bg-surface-3 [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-[12px] [&_h1]:mb-2 [&_h1]:text-lg [&_h1]:font-semibold [&_h2]:mb-1.5 [&_h2]:mt-4 [&_h2]:text-[15px] [&_h2]:font-semibold [&_h3]:mb-1 [&_h3]:mt-3 [&_h3]:font-semibold [&_li]:my-0.5 [&_ol]:list-decimal [&_ol]:pl-5 [&_p]:my-2 [&_pre]:overflow-x-auto [&_table]:my-2 [&_table]:w-full [&_td]:border [&_td]:border-border [&_td]:px-2 [&_td]:py-1 [&_th]:border [&_th]:border-border [&_th]:bg-surface-2 [&_th]:px-2 [&_th]:py-1 [&_th]:text-left [&_ul]:list-disc [&_ul]:pl-5">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSanitize]}
        urlTransform={(url) => (isHttp(url) ? url : '')}
        components={{
          a: ({ href, children }) =>
            isHttp(href) ? (
              <a href={href} target="_blank" rel="noopener noreferrer nofollow" className="text-brand underline-offset-2 hover:underline">
                {children}
              </a>
            ) : (
              <span>{children}</span>
            ),
          img: ({ alt }) => <span className="text-text-faint">[image: {alt || 'not loaded'}]</span>,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
}
