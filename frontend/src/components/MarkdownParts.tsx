import {
  Children,
  createContext,
  isValidElement,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ComponentProps,
  type ReactNode,
} from 'react'
import { AlertTriangle, Info, Lightbulb, MessageSquareWarning, OctagonAlert } from 'lucide-react'
import type { ExtraProps } from 'react-markdown'
import type { AlertKind } from '../markdownAlerts'

export function childText(children: ReactNode): string {
  return Children.toArray(children)
    .map((child) => {
      if (typeof child === 'string' || typeof child === 'number') return String(child)
      if (isValidElement<{ children?: ReactNode }>(child)) return childText(child.props.children)
      return ''
    })
    .join('')
}

/** Images among rendered Markdown children, overridden components included. */
function imageCount(children: ReactNode): number {
  return Children.toArray(children).reduce<number>((count, child) => {
    if (!isValidElement<{ children?: ReactNode } & ExtraProps>(child)) return count
    if (child.type === 'img' || child.props.node?.tagName === 'img') return count + 1
    return count + imageCount(child.props.children)
  }, 0)
}

const ReportBrokenImage = createContext<(() => void) | null>(null)

/** A Markdown image that leaves the page when it cannot load, instead of the
 * browser's broken-image mark and alt text (an offline server reaching for a
 * badge on the internet, say). */
export function MarkdownImage({ node: _node, onError, ...props }: ComponentProps<'img'> & ExtraProps) {
  const report = useContext(ReportBrokenImage)
  const [broken, setBroken] = useState(false)
  // An address the page refused leaves no source at all, and an image without
  // one never reports an error; it counts as broken from the start.
  const missing = !props.src
  useEffect(() => {
    if (missing) report?.()
  }, [missing, report])
  if (broken || missing) return null
  return (
    <img
      {...props}
      onError={(event) => {
        setBroken(true)
        report?.()
        onError?.(event)
      }}
    />
  )
}

/** Wraps a link whose content is `content`; when it held only images and none
 * of them loaded, the link goes too, so no empty link box is left behind. */
export function DropWhenImagesFail({ content, children }: { content: ReactNode; children: ReactNode }) {
  const [broken, setBroken] = useState(0)
  const report = useCallback(() => setBroken((count) => count + 1), [])
  const images = imageCount(content)
  if (images > 0 && broken >= images && !childText(content).trim()) return null
  return <ReportBrokenImage.Provider value={report}>{children}</ReportBrokenImage.Provider>
}

const ALERT_ICONS: Record<AlertKind, typeof Info> = {
  note: Info,
  tip: Lightbulb,
  important: MessageSquareWarning,
  warning: AlertTriangle,
  caution: OctagonAlert,
}

/** Paragraphs, with an icon on the title of a GitHub alert callout. */
export function MarkdownParagraph({ node: _node, children, ...props }: ComponentProps<'p'> & ExtraProps) {
  const kind = (props as Record<string, unknown>)['data-alert'] as AlertKind | undefined
  const Icon = kind ? ALERT_ICONS[kind] : undefined
  return (
    <p {...props}>
      {Icon && <Icon size={15} aria-hidden="true" />}
      {children}
    </p>
  )
}
