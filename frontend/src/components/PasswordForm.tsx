import { useEffect, useRef, useState, type FormEvent } from 'react'
import { AlertCircle, Check, KeyRound, LoaderCircle } from 'lucide-react'

const MIN_LENGTH = 12

/**
 * A new password typed twice, plus the current one when people change their
 * own. What happens is said inside the form: resolving `onSubmit` clears the
 * fields and shows `doneMessage`, which takes focus; rejecting shows the error
 * and keeps the fields, so a typo in the current password is quick to fix;
 * resolving `false` (a declined confirmation) keeps them and says nothing.
 */
export function PasswordForm({
  askCurrent,
  submitLabel,
  doneMessage,
  onSubmit,
}: {
  askCurrent: boolean
  submitLabel: string
  doneMessage: string
  onSubmit: (next: string, current: string) => Promise<boolean | void>
}) {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [again, setAgain] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState(false)
  const doneNote = useRef<HTMLParagraphElement>(null)
  const mismatch = again.length > 0 && again !== next
  const short = next.length > 0 && next.length < MIN_LENGTH

  // The fields empty and the button disables, so focus goes to what happened.
  useEffect(() => {
    if (done) doneNote.current?.focus()
  }, [done])

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (next !== again || next.length < MIN_LENGTH) return
    setSaving(true)
    setError('')
    setDone(false)
    try {
      if ((await onSubmit(next, current)) === false) return
      setCurrent('')
      setNext('')
      setAgain('')
      setDone(true)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'The password was not changed.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <form className="account-form" onSubmit={submit} onChange={() => setDone(false)}>
      {askCurrent && (
        <label>
          Current password
          <input
            type="password"
            autoComplete="current-password"
            value={current}
            onChange={(event) => setCurrent(event.target.value)}
            required
          />
        </label>
      )}
      <label>
        New password <small>at least {MIN_LENGTH} characters</small>
        <input
          type="password"
          autoComplete="new-password"
          minLength={MIN_LENGTH}
          value={next}
          onChange={(event) => setNext(event.target.value)}
          aria-invalid={short || undefined}
          required
        />
      </label>
      <label>
        Confirm new password
        <input
          type="password"
          autoComplete="new-password"
          value={again}
          onChange={(event) => setAgain(event.target.value)}
          aria-invalid={mismatch || undefined}
          required
        />
      </label>
      {mismatch && <p className="field-hint problem">The two new passwords do not match.</p>}
      {error && (
        <div className="inline-error form-message" role="alert">
          <AlertCircle size={15} /> {error}
        </div>
      )}
      {done && (
        <p ref={doneNote} className="form-message form-done" role="status" tabIndex={-1}>
          <Check size={15} /> {doneMessage}
        </p>
      )}
      <button className="download-button" disabled={saving || mismatch || short || !next || !again || (askCurrent && !current)}>
        {saving ? <LoaderCircle size={16} className="spin" /> : <KeyRound size={16} />} {submitLabel}
      </button>
    </form>
  )
}
