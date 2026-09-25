import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import {
  AlertCircle,
  Check,
  ChevronDown,
  ChevronUp,
  FolderOpen,
  LoaderCircle,
  RotateCcw,
  UploadCloud,
  X,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { api } from './api'
import { formatBytes } from './utils'

export interface UploadItem {
  file: File
  path: string
}

type JobStatus =
  | 'queued'
  | 'uploading'
  | 'committing'
  | 'done'
  | 'error'
  | 'cancelled'
  | 'interrupted'

interface UploadJob {
  id: string
  kind: 'new' | 'change'
  repoId: string
  items: UploadItem[]
  expected: Array<{ path: string; size: number; source: string }>
  deletions: string[]
  message: string
  description: string
  status: JobStatus
  uploaded: Record<string, number>
  total: number
  currentFile?: string
  error?: string
  sessionId?: string
}

interface NewUploadJob {
  kind: 'new' | 'change'
  repoId: string
  items: UploadItem[]
  deletions?: string[]
  message: string
  description?: string
}

interface UploadContextValue {
  jobs: UploadJob[]
  active: boolean
  enqueue: (job: NewUploadJob) => void
}

type ToastHandler = (message: string, tone?: 'success' | 'error') => void

const STORAGE_KEY = 'hugginghack-uploads'
const UNFINISHED: JobStatus[] = ['queued', 'uploading', 'committing', 'interrupted', 'error']
const UploadContext = createContext<UploadContextValue | null>(null)

export function useUploads(): UploadContextValue {
  const value = useContext(UploadContext)
  if (!value) throw new Error('useUploads must be used inside UploadProvider')
  return value
}

function uploadedBytes(job: UploadJob): number {
  return Object.values(job.uploaded).reduce((sum, value) => sum + value, 0)
}

function restoreJobs(): UploadJob[] {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    const saved = raw ? (JSON.parse(raw) as UploadJob[]) : []
    return saved.map((job) => ({
      ...job,
      items: [],
      uploaded: {},
      status: 'interrupted' as const,
      error: undefined,
    }))
  } catch {
    return []
  }
}

function persistJobs(jobs: UploadJob[]): void {
  try {
    const unfinished = jobs
      .filter((job) => UNFINISHED.includes(job.status))
      .map(({ items: _items, uploaded: _uploaded, ...rest }) => ({ ...rest, items: [], uploaded: {} }))
    if (unfinished.length) window.localStorage.setItem(STORAGE_KEY, JSON.stringify(unfinished))
    else window.localStorage.removeItem(STORAGE_KEY)
  } catch {
    // Remembering interrupted uploads is a convenience only.
  }
}

export function relativeUploadPath(file: File): string {
  const relative = file.webkitRelativePath || file.name
  const parts = relative.split('/').filter(Boolean)
  return parts.length > 1 ? parts.slice(1).join('/') : parts[0]
}

export function UploadProvider({
  children,
  onToast,
}: {
  children: ReactNode
  onToast: ToastHandler
}) {
  const [jobs, setJobs] = useState<UploadJob[]>(restoreJobs)
  const [minimized, setMinimized] = useState(false)
  const jobsRef = useRef(jobs)
  const controllers = useRef(new Map<string, AbortController>())
  const running = useRef(false)
  const chunkBytes = useRef(8 * 1024 * 1024)

  useEffect(() => {
    jobsRef.current = jobs
    persistJobs(jobs)
  }, [jobs])

  useEffect(() => {
    api
      .health()
      .then((health) => {
        chunkBytes.current = health.upload_chunk_bytes || chunkBytes.current
      })
      .catch(() => undefined)
  }, [])

  const update = useCallback((id: string, changes: Partial<UploadJob>) => {
    setJobs((current) => current.map((job) => (job.id === id ? { ...job, ...changes } : job)))
  }, [])

  const active = jobs.some((job) => ['queued', 'uploading', 'committing'].includes(job.status))

  useEffect(() => {
    if (!active) return
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [active])

  const run = useCallback(
    async (job: UploadJob) => {
      const controller = new AbortController()
      controllers.current.set(job.id, controller)
      let sessionId = job.sessionId
      try {
        update(job.id, { status: 'uploading', error: undefined })
        if (job.kind === 'change' && !sessionId) {
          sessionId = (await api.startChange(job.repoId)).id
          update(job.id, { sessionId })
        }
        for (const item of job.items) {
          update(job.id, { currentFile: item.path })
          const progress = (bytes: number) =>
            setJobs((current) =>
              current.map((entry) =>
                entry.id === job.id
                  ? { ...entry, uploaded: { ...entry.uploaded, [item.path]: bytes } }
                  : entry,
              ),
            )
          if (job.kind === 'change' && sessionId) {
            await api.uploadChangeFile(
              sessionId, item.path, item.file, chunkBytes.current, progress, controller.signal,
            )
          } else {
            await api.uploadFile(
              job.repoId, item.path, item.file, chunkBytes.current, progress, controller.signal,
            )
          }
        }
        update(job.id, { status: 'committing', currentFile: undefined })
        if (job.kind === 'change' && sessionId) {
          await api.commitChange(sessionId, {
            message: job.message,
            description: job.description,
            deletions: job.deletions,
          })
        } else {
          await api.finalizeUpload(job.repoId, {
            message: job.message,
            description: job.description,
          })
        }
        update(job.id, { status: 'done' })
        onToast(`${job.repoId}: “${job.message}” was committed.`)
        window.dispatchEvent(new CustomEvent('hugginghack:repository-changed', { detail: job.repoId }))
      } catch (reason) {
        if (controller.signal.aborted) {
          update(job.id, { status: 'cancelled', currentFile: undefined })
        } else {
          const message = reason instanceof Error ? reason.message : 'Upload failed'
          update(job.id, { status: 'error', error: message, currentFile: undefined })
          onToast(`${job.repoId}: ${message} Progress is saved; retry to resume.`, 'error')
        }
      } finally {
        controllers.current.delete(job.id)
      }
    },
    [onToast, update],
  )

  // Run queued jobs one at a time, whatever page is open.
  useEffect(() => {
    if (running.current) return
    const next = jobs.find((job) => job.status === 'queued')
    if (!next) return
    running.current = true
    run(next).finally(() => {
      running.current = false
      setJobs((current) => [...current])
    })
  }, [jobs, run])

  const enqueue = useCallback((job: NewUploadJob) => {
    const id = crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`
    setJobs((current) => [
      ...current,
      {
        id,
        kind: job.kind,
        repoId: job.repoId,
        items: job.items,
        expected: job.items.map((item) => ({
          path: item.path,
          size: item.file.size,
          source: relativeUploadPath(item.file),
        })),
        deletions: job.deletions || [],
        message: job.message,
        description: job.description || '',
        status: 'queued',
        uploaded: {},
        total: job.items.reduce((sum, item) => sum + item.file.size, 0),
      },
    ])
    setMinimized(false)
  }, [])

  async function cancel(job: UploadJob) {
    controllers.current.get(job.id)?.abort()
    if (job.kind === 'change' && job.sessionId) {
      await api.abortChange(job.sessionId).catch(() => undefined)
    }
    update(job.id, { status: 'cancelled', sessionId: undefined })
  }

  function reattach(job: UploadJob, files: File[]) {
    const byPath = new Map(files.map((file) => [relativeUploadPath(file), file]))
    const items: UploadItem[] = []
    for (const expected of job.expected) {
      const file = byPath.get(expected.source)
      if (!file || file.size !== expected.size) {
        update(job.id, {
          error: `The selected folder does not contain ${expected.source} (${formatBytes(expected.size)}).`,
        })
        return
      }
      items.push({ file, path: expected.path })
    }
    update(job.id, { items, status: 'queued', error: undefined })
  }

  const value = useMemo(() => ({ jobs, active, enqueue }), [jobs, active, enqueue])
  const visible = jobs
  const current = jobs.find((job) => job.status === 'uploading' || job.status === 'committing')
  const totals = jobs
    .filter((job) => UNFINISHED.includes(job.status))
    .reduce(
      (sum, job) => ({ done: sum.done + uploadedBytes(job), total: sum.total + job.total }),
      { done: 0, total: 0 },
    )
  const percent = totals.total ? Math.min(100, (totals.done / totals.total) * 100) : 100

  return (
    <UploadContext.Provider value={value}>
      {children}
      {visible.length > 0 && (
        <aside
          className={minimized ? 'upload-dock minimized' : 'upload-dock'}
          aria-label="Uploads"
          aria-live="polite"
        >
          <header className="upload-dock-header">
            <button
              type="button"
              className="upload-dock-toggle"
              onClick={() => setMinimized(!minimized)}
              aria-expanded={!minimized}
            >
              {active ? <LoaderCircle size={15} className="spin" /> : <UploadCloud size={15} />}
              <span>
                {active
                  ? `Uploading${current ? ` ${current.repoId}` : ''} · ${percent.toFixed(0)}%`
                  : `${visible.length} upload${visible.length === 1 ? '' : 's'}`}
              </span>
              {minimized ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
            </button>
            {!active && (
              <button
                type="button"
                className="icon-button"
                aria-label="Clear finished uploads"
                title="Clear finished uploads"
                onClick={() =>
                  setJobs((all) => all.filter((job) => ['interrupted', 'error'].includes(job.status)))
                }
              >
                <X size={16} />
              </button>
            )}
          </header>
          {minimized ? (
            active && (
              <div className="job-progress upload-dock-mini-progress">
                <span style={{ width: `${percent}%` }} />
              </div>
            )
          ) : (
            <ul className="upload-dock-list">
              {visible.map((job) => {
                const done = uploadedBytes(job)
                const jobPercent = job.total ? Math.min(100, (done / job.total) * 100) : 0
                return (
                  <li key={job.id} className={`upload-job ${job.status}`}>
                    <div className="upload-job-title">
                      <Link to={`/models/${job.repoId}`}>{job.repoId}</Link>
                      <span>{job.message}</span>
                    </div>
                    {['uploading', 'queued', 'committing'].includes(job.status) && (
                      <>
                        <div className="job-progress">
                          <span style={{ width: `${job.status === 'committing' ? 100 : jobPercent}%` }} />
                        </div>
                        <div className="upload-job-meta">
                          <span>
                            {job.status === 'queued'
                              ? 'Waiting'
                              : job.status === 'committing'
                                ? 'Committing…'
                                : job.currentFile || 'Starting…'}
                          </span>
                          <span>
                            {formatBytes(done)} / {formatBytes(job.total)}
                          </span>
                        </div>
                      </>
                    )}
                    {job.status === 'done' && (
                      <p className="upload-job-state ok">
                        <Check size={13} /> Committed {job.items.length} file
                        {job.items.length === 1 ? '' : 's'}
                        {job.deletions.length ? `, deleted ${job.deletions.length}` : ''}
                      </p>
                    )}
                    {job.status === 'cancelled' && (
                      <p className="upload-job-state">Cancelled; staged files were discarded.</p>
                    )}
                    {job.error && (
                      <p className="upload-job-state danger">
                        <AlertCircle size={13} /> {job.error}
                      </p>
                    )}
                    {job.status === 'interrupted' && (
                      <p className="upload-job-state">
                        The page was reloaded. Choose the same folder again to resume where it stopped.
                      </p>
                    )}
                    <div className="upload-job-actions">
                      {['uploading', 'queued'].includes(job.status) && (
                        <button type="button" onClick={() => cancel(job)}>
                          <X size={13} /> Cancel
                        </button>
                      )}
                      {job.status === 'error' && (
                        <button type="button" onClick={() => update(job.id, { status: 'queued' })}>
                          <RotateCcw size={13} /> Retry
                        </button>
                      )}
                      {job.status === 'interrupted' && (
                        <label className="upload-job-reselect">
                          <FolderOpen size={13} /> Choose folder
                          <input
                            type="file"
                            multiple
                            ref={(input) => {
                              input?.setAttribute('webkitdirectory', '')
                              input?.setAttribute('directory', '')
                            }}
                            onChange={(event) => reattach(job, Array.from(event.target.files || []))}
                          />
                        </label>
                      )}
                      {['interrupted', 'error'].includes(job.status) && (
                        <button type="button" onClick={() => cancel(job).then(() => setJobs((all) => all.filter((entry) => entry.id !== job.id)))}>
                          Discard
                        </button>
                      )}
                    </div>
                  </li>
                )
              })}
            </ul>
          )}
        </aside>
      )}
    </UploadContext.Provider>
  )
}
