import { useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react'
import { Bold, Code, Heading2, Italic, Link2, List, ListOrdered } from 'lucide-react'
import ReactMarkdown, { type Components } from 'react-markdown'
import rehypeSanitize from 'rehype-sanitize'
import remarkGfm from 'remark-gfm'
import { rehypeGithubAlerts } from '../markdownAlerts'
import { applyFormat, type MarkdownFormat } from '../markdownText'
import { useFadeOnChange, useTabIndicator } from '../motion'
import { DropWhenImagesFail, MarkdownImage, MarkdownParagraph } from './MarkdownParts'

const components: Components = {
  a: ({ node: _node, href, children, ...props }) => {
    const external = /^https?:/i.test(href || '')
    return (
      <DropWhenImagesFail content={children}>
        <a {...props} href={href} target={external ? '_blank' : undefined} rel={external ? 'noreferrer noopener' : undefined}>
          {children}
        </a>
      </DropWhenImagesFail>
    )
  },
  img: MarkdownImage,
  p: MarkdownParagraph,
}

/**
 * Markdown people write on the site, shown the way model cards are. Raw HTML is
 * not rendered and every link and image address is checked.
 */
export function MarkdownText({ source, className }: { source: string; className?: string }) {
  return (
    <div className={['model-card-document', 'markdown-text', className].filter(Boolean).join(' ')}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize, rehypeGithubAlerts]} components={components}>
        {source}
      </ReactMarkdown>
    </div>
  )
}

const TOOLS: Array<{ format: MarkdownFormat; label: string; icon: typeof Bold; keys?: string }> = [
  { format: 'bold', label: 'Bold', icon: Bold, keys: 'B' },
  { format: 'italic', label: 'Italic', icon: Italic, keys: 'I' },
  { format: 'heading', label: 'Heading', icon: Heading2 },
  { format: 'bullets', label: 'Bulleted list', icon: List },
  { format: 'numbers', label: 'Numbered list', icon: ListOrdered },
  { format: 'link', label: 'Link', icon: Link2, keys: 'K' },
  { format: 'code', label: 'Code', icon: Code },
]
const SHORTCUTS: Record<string, MarkdownFormat> = { b: 'bold', i: 'italic', k: 'link' }

/** A text area for Markdown with a formatting bar and a preview of the result. */
export function MarkdownEditor({
  value,
  onChange,
  label,
  maxLength,
  placeholder,
  rows = 8,
}: {
  value: string
  onChange: (value: string) => void
  label: string
  maxLength: number
  placeholder?: string
  rows?: number
}) {
  const [mode, setMode] = useState<'write' | 'preview'>('write')
  const area = useRef<HTMLTextAreaElement>(null)
  // Where the cursor goes once a formatting change has been written.
  const selection = useRef<[number, number] | null>(null)
  const tabs = useTabIndicator<HTMLDivElement>(mode)
  const body = useFadeOnChange<HTMLDivElement>(mode)

  function format(kind: MarkdownFormat) {
    const element = area.current
    if (!element) return
    const result = applyFormat(value, element.selectionStart, element.selectionEnd, kind)
    if (result.text.length > maxLength) return
    selection.current = [result.start, result.end]
    onChange(result.text)
  }

  // Before the next keystroke can land, put the cursor where the format left it.
  useLayoutEffect(() => {
    const element = area.current
    if (!selection.current || !element) return
    element.focus()
    element.setSelectionRange(...selection.current)
    selection.current = null
  }, [value])

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    const kind = (event.metaKey || event.ctrlKey) && !event.altKey ? SHORTCUTS[event.key.toLowerCase()] : undefined
    if (kind) {
      event.preventDefault()
      format(kind)
    }
  }

  return (
    <div className="markdown-editor">
      <div className="markdown-editor-bar">
        <span className="markdown-editor-label">{label}</span>
        <div className="markdown-editor-tabs" ref={tabs} role="tablist" aria-label={`${label} view`}>
          {(['write', 'preview'] as const).map((item) => (
            <button
              key={item}
              type="button"
              role="tab"
              aria-selected={mode === item}
              className={mode === item ? 'active' : undefined}
              onClick={() => setMode(item)}
            >
              {item === 'write' ? 'Write' : 'Preview'}
            </button>
          ))}
        </div>
      </div>
      <div ref={body} className="markdown-editor-body">
        {mode === 'write' ? (
          <>
            <div className="markdown-toolbar" role="toolbar" aria-label="Formatting">
              {TOOLS.map(({ format: kind, label: name, icon: Icon, keys }) => (
                <button
                  key={kind}
                  type="button"
                  title={keys ? `${name} (⌘${keys})` : name}
                  aria-label={name}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => format(kind)}
                >
                  <Icon size={15} />
                </button>
              ))}
            </div>
            <textarea
              ref={area}
              value={value}
              onChange={(event) => onChange(event.target.value)}
              onKeyDown={onKeyDown}
              maxLength={maxLength}
              rows={rows}
              placeholder={placeholder}
              aria-label={label}
            />
            <div className="markdown-editor-foot">
              <span>Markdown: **bold**, _italic_, ## heading, - list, [link](https://…)</span>
              <span>{value.length.toLocaleString('en')} / {maxLength.toLocaleString('en')}</span>
            </div>
          </>
        ) : value.trim() ? (
          <MarkdownText source={value} className="markdown-preview" />
        ) : (
          <p className="markdown-empty">Nothing to preview yet.</p>
        )}
      </div>
    </div>
  )
}
