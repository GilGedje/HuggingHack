import { useState, type FormEvent } from 'react'
import { KeyRound, LoaderCircle } from 'lucide-react'

const MIN_LENGTH = 12

/**
 * A new password typed twice, plus the current one when people change their
 * own. Resolving `onSubmit` clears the fields; rejecting keeps them so a typo
 * in the current password is quick to fix.
 */
export function PasswordForm({
  askCurrent,
  submitLabel,
  onSubmit,
}: {
  askCurrent: boolean
  submitLabel: string
  onSubmit: (next: string, current: string) => Promise<void>
}) {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [again, setAgain] = useState('')
  const [saving, setSaving] = useState(false)
  const mismatch = again.length > 0 && again !== next
  const short = next.length > 0 && next.length < MIN_LENGTH

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (next !== again || next.length < MIN_LENGTH) return
    setSaving(true)
    try {
      await onSubmit(next, current)
      setCurrent('')
      setNext('')
      setAgain('')
    } catch {
      // The caller reports the failure.
    } finally {
      setSaving(false)
    }
  }

  return (
    <form className="account-form" onSubmit={submit}>
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
      <button className="download-button" disabled={saving || mismatch || short || !next || !again || (askCurrent && !current)}>
        {saving ? <LoaderCircle size={16} className="spin" /> : <KeyRound size={16} />} {submitLabel}
      </button>
    </form>
  )
}
