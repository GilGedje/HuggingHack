import type { CommitChange } from '../types'
import { formatBytes } from '../utils'

function diffClass(line: string): string {
  if (line.startsWith('+++') || line.startsWith('---')) return 'meta'
  if (line.startsWith('@@')) return 'hunk'
  if (line.startsWith('+')) return 'add'
  if (line.startsWith('-')) return 'del'
  return ''
}

/** One changed file of a commit or config revision, with its unified diff. */
export function FileDiff({ change }: { change: CommitChange }) {
  return (
    <article className={`commit-file ${change.change}`}>
      <header>
        <span className={`commit-change-kind ${change.change}`}>{change.change}</span>
        <code>{change.path}</code>
        <span className="commit-file-stats">
          {change.binary ? (
            <>
              {change.old_size != null && formatBytes(change.old_size)}
              {change.old_size != null && change.new_size != null && ' → '}
              {change.new_size != null && formatBytes(change.new_size)}
            </>
          ) : (
            <>
              <span className="added">+{change.additions || 0}</span>{' '}
              <span className="deleted">−{change.deletions || 0}</span>
            </>
          )}
        </span>
      </header>
      {change.binary ? (
        <p className="commit-binary">
          Binary or large file; contents are not kept in history.
        </p>
      ) : (
        <pre className="commit-diff">
          {(change.diff || []).map((line, index) => (
            <span key={index} className={diffClass(line)}>
              {line || ' '}
              {'\n'}
            </span>
          ))}
          {change.truncated && <span className="hunk">… diff truncated</span>}
        </pre>
      )}
    </article>
  )
}
