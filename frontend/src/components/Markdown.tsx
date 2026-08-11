import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Assistant answers come back as markdown: tables of resources, CLI snippets, and links
 * into the AWS docs it cited. Tailwind resets all of that, so the elements are styled
 * here rather than pulling in a typography plugin for one view.
 */
export default function Markdown({ children }: { children: string }) {
  return (
    <div className="space-y-3 text-sm leading-relaxed text-ink">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          p: ({ children }) => <p className="text-ink-muted">{children}</p>,
          h1: ({ children }) => <h3 className="text-sm font-semibold text-ink">{children}</h3>,
          h2: ({ children }) => <h3 className="text-sm font-semibold text-ink">{children}</h3>,
          h3: ({ children }) => <h4 className="text-sm font-semibold text-ink">{children}</h4>,
          ul: ({ children }) => (
            <ul className="list-disc space-y-1 pl-5 text-ink-muted">{children}</ul>
          ),
          ol: ({ children }) => (
            <ol className="list-decimal space-y-1 pl-5 text-ink-muted">{children}</ol>
          ),
          strong: ({ children }) => <strong className="font-semibold text-ink">{children}</strong>,
          a: ({ children, href }) => (
            <a
              href={href}
              target="_blank"
              rel="noreferrer"
              className="text-accent underline underline-offset-2"
            >
              {children}
            </a>
          ),
          code: ({ children, className }) =>
            className ? (
              // Fenced block: the <pre> below provides the frame.
              <code className="font-mono text-xs text-ink">{children}</code>
            ) : (
              <code className="rounded bg-surface-2 px-1 py-0.5 font-mono text-xs text-ink">
                {children}
              </code>
            ),
          pre: ({ children }) => (
            <pre className="overflow-x-auto rounded-lg border border-line/70 bg-canvas px-3 py-2">
              {children}
            </pre>
          ),
          table: ({ children }) => (
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-xs">{children}</table>
            </div>
          ),
          th: ({ children }) => (
            <th className="border-b border-line/70 px-2 py-1.5 text-left font-medium text-ink-faint">
              {children}
            </th>
          ),
          td: ({ children }) => (
            <td className="border-b border-line/40 px-2 py-1.5 text-ink-muted">{children}</td>
          ),
          blockquote: ({ children }) => (
            <blockquote className="border-l-2 border-line pl-3 text-ink-faint">
              {children}
            </blockquote>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
