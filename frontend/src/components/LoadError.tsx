import { AlertCircle } from 'lucide-react'

/** A list or page that could not be read, in place of looking empty, with a way to try again. */
export function LoadError({ what, message, onRetry }: { what: string; message: string; onRetry: () => void }) {
  return (
    <div className="page-error" role="alert">
      <AlertCircle size={18} />
      <div>
        <strong>Could not load {what}</strong>
        <p>{message}</p>
      </div>
      <button type="button" onClick={onRetry}>Retry</button>
    </div>
  )
}
