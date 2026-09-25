import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'

const FOCUSABLE =
  'a[href], button:not(:disabled), input:not(:disabled):not([type="hidden"]), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])'

interface DialogFrameProps {
  /** Id of the element that names the dialog. */
  labelledBy: string
  /** Extra class on the panel, next to `use-model-dialog`. */
  className?: string
  /** From `useClosingTransition`: the dialog is playing its exit. */
  closing: boolean
  /** Escape and a click on the backdrop call this; pass `close` from `useClosingTransition`. */
  onDismiss: () => void
  /** Renders the panel as a form. */
  onSubmit?: (event: FormEvent<HTMLFormElement>) => void
  children: ReactNode
}

/**
 * Backdrop and panel shared by every dialog: Escape and backdrop clicks dismiss
 * it, the exit animation plays while `closing`, Tab stays inside the panel, and
 * focus goes back to whatever opened the dialog once it is gone.
 */
export function DialogFrame({ labelledBy, className, closing, onDismiss, onSubmit, children }: DialogFrameProps) {
  // Read during the first render, before the dialog moves focus inside itself.
  const [opener] = useState(() => document.activeElement as HTMLElement | null)
  const backdrop = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      // With a confirmation open over this dialog, the keys belong to it.
      const open = document.querySelectorAll('.use-model-backdrop')
      if (open[open.length - 1] !== backdrop.current) return
      if (event.key === 'Tab') {
        keepFocusInside(event)
        return
      }
      if (event.key !== 'Escape') return
      event.stopPropagation()
      onDismiss()
    }
    // Tab past either end wraps around the panel instead of reaching the page behind it.
    const keepFocusInside = (event: KeyboardEvent) => {
      const panel = backdrop.current?.firstElementChild
      if (!panel) return
      const items = [...panel.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((item) => item.getClientRects().length)
      if (!items.length) return
      const first = items[0]
      const last = items[items.length - 1]
      const current = document.activeElement
      if (!panel.contains(current)) {
        event.preventDefault()
        ;(event.shiftKey ? last : first).focus()
      } else if (event.shiftKey && current === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && current === last) {
        event.preventDefault()
        first.focus()
      }
    }
    window.addEventListener('keydown', onKeyDown, true)
    return () => window.removeEventListener('keydown', onKeyDown, true)
  }, [onDismiss])

  useEffect(
    () => () => {
      if (opener?.isConnected && opener !== document.body) opener.focus({ preventScroll: true })
    },
    [opener],
  )

  const panel = {
    className: className ? `use-model-dialog ${className}` : 'use-model-dialog',
    role: 'dialog',
    'aria-modal': true,
    'aria-labelledby': labelledBy,
    onMouseDown: (event: React.MouseEvent) => event.stopPropagation(),
  } as const

  return (
    <div ref={backdrop} className={closing ? 'use-model-backdrop closing' : 'use-model-backdrop'} role="presentation" onMouseDown={onDismiss}>
      {onSubmit ? (
        <form {...panel} onSubmit={onSubmit}>{children}</form>
      ) : (
        <section {...panel}>{children}</section>
      )}
    </div>
  )
}
