import { createContext, useCallback, useContext, useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { AlertTriangle, X } from 'lucide-react'
import { useClosingTransition } from '../motion'
import { DialogFrame } from './Dialog'

export interface ConfirmOptions {
  /** A short question, like "Delete Qwen?" */
  title: string
  /** What happens, and whether it can be undone. */
  message?: ReactNode
  /** The button that goes ahead, named for what it does: "Delete", "Revoke". */
  confirmLabel: string
  /** Destructive actions get the danger button and a warning mark. */
  danger?: boolean
  /** Ask for this text to be typed before going ahead, for what cannot be undone. */
  requireText?: string
  /** A small line above the title. */
  eyebrow?: string
}

type Ask = (options: ConfirmOptions) => Promise<boolean>

const ConfirmContext = createContext<Ask | null>(null)

/**
 * Asks before doing something that matters, in the site's own dialog instead of
 * the browser's. `await confirm({...})` is true when the person goes ahead.
 */
export function useConfirm(): Ask {
  const ask = useContext(ConfirmContext)
  if (!ask) throw new Error('useConfirm needs a ConfirmProvider.')
  return ask
}

interface Pending extends ConfirmOptions {
  id: number
  resolve: (value: boolean) => void
}

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<Pending | null>(null)
  const counter = useRef(0)

  const ask = useCallback<Ask>(
    (options) =>
      new Promise<boolean>((resolve) => {
        counter.current += 1
        setPending((current) => {
          // A new question replaces one left open; the old one counts as a no.
          current?.resolve(false)
          return { ...options, id: counter.current, resolve }
        })
      }),
    [],
  )

  return (
    <ConfirmContext.Provider value={ask}>
      {children}
      {pending && (
        <ConfirmDialog
          key={pending.id}
          options={pending}
          onDone={(value) => {
            pending.resolve(value)
            setPending((current) => (current?.id === pending.id ? null : current))
          }}
        />
      )}
    </ConfirmContext.Provider>
  )
}

function ConfirmDialog({ options, onDone }: { options: ConfirmOptions; onDone: (value: boolean) => void }) {
  const answer = useRef(false)
  const { closing, close } = useClosingTransition(() => onDone(answer.current))
  const [typed, setTyped] = useState('')
  const field = useRef<HTMLInputElement>(null)
  const cancelButton = useRef<HTMLButtonElement>(null)
  const ready = !options.requireText || typed === options.requireText

  // Typing comes first when it is asked for; otherwise the safe choice has focus.
  useEffect(() => {
    ;(options.requireText ? field.current : cancelButton.current)?.focus({ preventScroll: true })
  }, [options.requireText])

  function finish(value: boolean) {
    answer.current = value
    close()
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    if (ready) finish(true)
  }

  return (
    <DialogFrame
      labelledBy="confirm-title"
      className={options.danger ? 'confirm-dialog danger' : 'confirm-dialog'}
      closing={closing}
      onDismiss={() => finish(false)}
      onSubmit={submit}
    >
      <header className="use-model-header">
        <div className="confirm-heading">
          {options.danger && (
            <span className="confirm-mark" aria-hidden="true">
              <AlertTriangle size={17} />
            </span>
          )}
          <div>
            {options.eyebrow && <span className="eyebrow">{options.eyebrow}</span>}
            <h2 id="confirm-title">{options.title}</h2>
          </div>
        </div>
        <button type="button" className="icon-button" onClick={() => finish(false)} aria-label="Close">
          <X size={20} />
        </button>
      </header>
      <div className="use-model-body confirm-body">
        {options.message && <div className="confirm-message">{options.message}</div>}
        {options.requireText && (
          <label className="wizard-label">
            <span>
              Type <code>{options.requireText}</code> to confirm
            </span>
            <input
              ref={field}
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
              autoComplete="off"
              spellCheck={false}
            />
          </label>
        )}
        <div className="add-user-footer">
          <span />
          <button ref={cancelButton} type="button" className="secondary-button" onClick={() => finish(false)}>
            Cancel
          </button>
          <button type="submit" className={options.danger ? 'danger-button' : 'download-button'} disabled={!ready}>
            {options.confirmLabel}
          </button>
        </div>
      </div>
    </DialogFrame>
  )
}
