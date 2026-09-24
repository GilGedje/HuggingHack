import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { FilePlus2, FolderUp, GitCommitHorizontal, Trash2, X } from 'lucide-react'
import type { LibraryModelDetails } from '../types'
import { relativeUploadPath, useUploads, type UploadItem } from '../uploads'
import { formatBytes } from '../utils'

interface UploadChangeDialogProps {
  model: LibraryModelDetails
  directory: string
  onClose: () => void
  onQueued: () => void
}

function joinPath(folder: string, path: string): string {
  const cleanFolder = folder.trim().replace(/^\/+|\/+$/g, '')
  return cleanFolder ? `${cleanFolder}/${path}` : path
}

export function UploadChangeDialog({ model, directory, onClose, onQueued }: UploadChangeDialogProps) {
  const { enqueue, jobs } = useUploads()
  const folderInput = useRef<HTMLInputElement>(null)
  const closeButton = useRef<HTMLButtonElement>(null)
  const [folder, setFolder] = useState(directory)
  const [picked, setPicked] = useState<Array<{ file: File; source: string }>>([])
  const [deletions, setDeletions] = useState<string[]>([])
  const [filter, setFilter] = useState('')
  const [message, setMessage] = useState('')
  const [description, setDescription] = useState('')

  useEffect(() => {
    folderInput.current?.setAttribute('webkitdirectory', '')
    folderInput.current?.setAttribute('directory', '')
    closeButton.current?.focus()
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', escape)
    return () => window.removeEventListener('keydown', escape)
  }, [onClose])

  const existing = useMemo(() => new Set(model.files.map((file) => file.path)), [model.files])
  const items: UploadItem[] = picked.map(({ file, source }) => ({ file, path: joinPath(folder, source) }))
  const invalid = items.find((item) =>
    item.path.split('/').some((part) => !part || part === '.' || part === '..' || part.startsWith('.')),
  )
  const busy = jobs.some(
    (job) => job.repoId === model.id && ['queued', 'uploading', 'committing'].includes(job.status),
  )
  const totalBytes = picked.reduce((sum, item) => sum + item.file.size, 0)
  const visibleFiles = model.files.filter((file) =>
    file.path.toLowerCase().includes(filter.trim().toLowerCase()),
  )
  const defaultMessage = items.length
    ? `Upload ${items.length} file${items.length === 1 ? '' : 's'}`
    : deletions.length
      ? `Delete ${deletions.length} file${deletions.length === 1 ? '' : 's'}`
      : 'Update files'

  function add(files: FileList | null, keepFolders: boolean) {
    const next = Array.from(files || []).map((file) => ({
      file,
      source: keepFolders ? relativeUploadPath(file) : file.name,
    }))
    setPicked((current) => {
      const bySource = new Map(current.map((item) => [item.source, item]))
      for (const item of next) bySource.set(item.source, item)
      return [...bySource.values()]
    })
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    if ((!items.length && !deletions.length) || invalid || busy) return
    enqueue({
      kind: 'change',
      repoId: model.id,
      items,
      deletions: deletions.filter((path) => !items.some((item) => item.path === path)),
      message: message.trim() || defaultMessage,
      description,
    })
    onQueued()
  }

  return (
    <div className="use-model-backdrop" role="presentation" onMouseDown={onClose}>
      <form
        className="use-model-dialog upload-change-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="upload-change-title"
        onMouseDown={(event) => event.stopPropagation()}
        onSubmit={submit}
      >
        <header className="use-model-header">
          <div>
            <span className="eyebrow">Commit a change</span>
            <h2 id="upload-change-title">{model.id}</h2>
          </div>
          <button ref={closeButton} type="button" className="icon-button" onClick={onClose} aria-label="Close">
            <X size={20} />
          </button>
        </header>
        <div className="use-model-body">
          <div className="upload-change-pickers">
            <label className="secondary-button">
              <FilePlus2 size={16} /> Add files
              <input type="file" multiple onChange={(event) => add(event.target.files, false)} />
            </label>
            <label className="secondary-button">
              <FolderUp size={16} /> Add folder
              <input ref={folderInput} type="file" multiple onChange={(event) => add(event.target.files, true)} />
            </label>
            <label className="upload-change-folder">
              Into folder
              <input
                value={folder}
                onChange={(event) => setFolder(event.target.value)}
                placeholder="repository root"
              />
            </label>
          </div>

          {items.length > 0 && (
            <ul className="upload-change-list">
              {items.map((item, index) => (
                <li key={item.path}>
                  <code title={item.path}>{item.path}</code>
                  <span className={existing.has(item.path) ? 'change-tag modified' : 'change-tag added'}>
                    {existing.has(item.path) ? 'replaces' : 'new'}
                  </span>
                  <small>{formatBytes(item.file.size)}</small>
                  <button
                    type="button"
                    aria-label={`Remove ${item.path}`}
                    onClick={() => setPicked((current) => current.filter((_, position) => position !== index))}
                  >
                    <X size={13} />
                  </button>
                </li>
              ))}
            </ul>
          )}
          {invalid && (
            <div className="inline-error">
              {invalid.path} is not a valid repository path. Hidden files and folders cannot be uploaded.
            </div>
          )}

          <details className="upload-change-delete">
            <summary>
              <Trash2 size={14} /> Delete existing files
              {deletions.length > 0 && <span>{deletions.length} selected</span>}
            </summary>
            <input
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
              placeholder="Filter files"
              aria-label="Filter files to delete"
            />
            <div className="upload-change-delete-list">
              {visibleFiles.map((file) => (
                <label key={file.path}>
                  <input
                    type="checkbox"
                    checked={deletions.includes(file.path)}
                    onChange={(event) =>
                      setDeletions((current) =>
                        event.target.checked
                          ? [...current, file.path]
                          : current.filter((path) => path !== file.path),
                      )
                    }
                  />
                  <code>{file.path}</code>
                  <small>{formatBytes(file.size)}</small>
                </label>
              ))}
            </div>
          </details>

          <div className="upload-change-commit">
            <label>
              Commit message
              <input
                value={message}
                onChange={(event) => setMessage(event.target.value)}
                placeholder={defaultMessage}
                maxLength={200}
              />
            </label>
            <label>
              Extended description <small>optional</small>
              <textarea
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                maxLength={5000}
                rows={3}
              />
            </label>
          </div>
          <div className="upload-change-footer">
            <span>
              {items.length} to upload · {formatBytes(totalBytes)}
              {deletions.length ? ` · ${deletions.length} to delete` : ''}
            </span>
            <button
              type="submit"
              className="download-button"
              disabled={(!items.length && !deletions.length) || Boolean(invalid) || busy}
            >
              <GitCommitHorizontal size={16} />
              {busy ? 'Another change is uploading' : 'Commit changes'}
            </button>
          </div>
          <p className="use-model-note">
            Files upload in the background and apply all at once when the commit finishes, so anyone
            pulling this model never sees a half-finished change.
          </p>
        </div>
      </form>
    </div>
  )
}
