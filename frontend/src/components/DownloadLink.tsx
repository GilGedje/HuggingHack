import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Check, Download } from 'lucide-react'

/** How long the link says the download started before it looks as it did. */
const STARTED_MS = 1600

/**
 * A download link that acknowledges the click: the browser saves the file
 * without leaving the page, so without this nothing on the page would answer.
 * The arrow turns into a check for a moment, then back.
 */
export function DownloadLink({
  href,
  className,
  label,
  iconSize = 14,
  children,
}: {
  href: string
  className?: string
  /** Accessible name when the link shows only its icon. */
  label?: string
  iconSize?: number
  children?: ReactNode
}) {
  const [started, setStarted] = useState(false)
  // Icons turn only once the link has been used, not when the list first appears.
  const [used, setUsed] = useState(false)
  const timer = useRef(0)
  useEffect(() => () => window.clearTimeout(timer.current), [])

  return (
    <a
      href={href}
      download
      className={[className, 'download-link', used && 'used', started && 'started'].filter(Boolean).join(' ')}
      aria-label={label}
      title={label}
      onClick={() => {
        setStarted(true)
        setUsed(true)
        window.clearTimeout(timer.current)
        timer.current = window.setTimeout(() => setStarted(false), STARTED_MS)
      }}
    >
      {started ? <Check size={iconSize} key="started" /> : <Download size={iconSize} key="ready" />}
      {children}
    </a>
  )
}
