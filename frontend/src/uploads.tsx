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
  FolderOpen,
  LoaderCircle,
  RotateCcw,
  UploadCloud,
  X,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { api } from './api'
import { uploadDirect } from './directUpload'
import type { UploadDestination } from './types'
import { useConfirm } from './components/ConfirmDialog'
import { DOCK_EXIT_MS, useFadeOnChange } from './motion'
import { isRecorded } from './uploadPlan'
import { LEASE_HEARTBEAT_MS, claimOrphans, parseTabRecord, type TabRecord } from './uploadStore'
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

// Before tabs kept their own records, every tab shared this one list.
const LEGACY_STORAGE_KEY = 'hugginghack-uploads'
const STORAGE_PREFIX = 'hugginghack-uploads:'
const UNFINISHED: JobStatus[] = ['queued', 'uploading', 'committing', 'interrupted', 'error']
const IN_PROGRESS: JobStatus[] = ['queued', 'uploading', 'committing']
// Fresh on every page load, so a reloaded tab takes over its own earlier jobs.
// randomUUID needs a secure context; plain-HTTP LAN servers get the fallback.
const TAB_ID = crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`
const OWN_KEY = `${STORAGE_PREFIX}${TAB_ID}`
// Records of gone tabs whose jobs this tab took over, removed once it has saved them.
const adoptedKeys = new Set<string>()
const UploadContext = createContext<UploadContextValue | null>(null)

export function useUploads(): UploadContextValue {
  const value = useContext(UploadContext)
  if (!value) throw new Error('useUploads must be used inside UploadProvider')
  return value
}

/** Every file of a job with its size and how much of it has arrived, as a
 * fraction; `null` when that is unknown (the page was reloaded mid-upload). */
function fileProgress(job: UploadJob) {
  const sorted = [...job.expected].sort((a, b) => a.path.localeCompare(b.path, undefined, { numeric: true }))
  return sorted.map((file) => {
    const sent = job.uploaded[file.path]
    const fraction =
      job.status === 'done' || job.status === 'committing'
        ? 1
        : sent != null
          ? file.size ? Math.min(1, sent / file.size) : 1
          : job.status === 'interrupted'
            ? null
            : 0
    const current = job.currentFile === file.path
    // Sending: under way now, or part-sent before a pause or an error.
    const sending = current || (fraction != null && fraction > 0 && fraction < 1)
    return { ...file, fraction, current, sending }
  })
}

/** The files of one job, folded open under it: name, size, and progress each. */
function JobFiles({ job, open, id }: { job: UploadJob; open: boolean; id: string }) {
  const files = fileProgress(job)
  return (
    <div
      id={id}
      className={open ? 'upload-dock-fold upload-job-files open' : 'upload-dock-fold upload-job-files'}
      ref={(element) => {
        if (open) element?.removeAttribute('inert')
        else element?.setAttribute('inert', '')
      }}
      aria-hidden={open ? undefined : true}
    >
      <div>
        <ul>
          {files.map((file) => (
            <li key={file.path} className={file.current ? 'current' : file.fraction === 1 ? 'complete' : undefined}>
              <span className="upload-file-name" title={file.path}>{file.path}</span>
              <span className="upload-file-size">
                {file.sending
                  ? `${formatBytes(file.size * (file.fraction ?? 0))} / ${formatBytes(file.size)}`
                  : formatBytes(file.size)}
              </span>
              <span className="upload-file-state">
                {file.fraction === 1 ? (
                  <Check size={12} aria-label="Sent" />
                ) : file.sending ? (
                  `${Math.floor((file.fraction ?? 0) * 100)}%`
                ) : (
                  <span className="sr-only">Waiting</span>
                )}
              </span>
              {/* Only a file on its way gets a bar; finished ones have their check. */}
              {file.sending && (
                <div className={file.current ? 'job-progress live' : 'job-progress'}>
                  <span style={{ width: `${(file.fraction ?? 0) * 100}%` }} />
                </div>
              )}
            </li>
          ))}
          {job.deletions.map((path) => (
            <li key={`deleted:${path}`} className="deleted">
              <span className="upload-file-name" title={path}>{path}</span>
              <span className="upload-file-size">deleted</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}

function uploadedBytes(job: UploadJob): number {
  return Object.values(job.uploaded).reduce((sum, value) => sum + value, 0)
}

/** Jobs left behind by tabs that are gone. Jobs another open tab is running
 * stay with that tab. */
function restoreJobs(): UploadJob[] {
  try {
    const records: Array<[string, TabRecord<UploadJob>]> = []
    for (let index = 0; index < window.localStorage.length; index += 1) {
      const key = window.localStorage.key(index)
      if (key !== LEGACY_STORAGE_KEY && !key?.startsWith(STORAGE_PREFIX)) continue
      records.push([key, parseTabRecord<UploadJob>(window.localStorage.getItem(key)) || { seen: 0, jobs: [] }])
    }
    const orphans = claimOrphans(records, OWN_KEY, Date.now())
    // Removed only after this tab saves the jobs, so a second call (StrictMode) finds them too.
    for (const key of orphans.keys) adoptedKeys.add(key)
    return orphans.jobs.map((job) => ({
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

/** Saves this tab's unfinished jobs under its own key, the only one it writes.
 * `seen` 0 tells other tabs this one is gone. */
function persistJobs(jobs: UploadJob[], seen = Date.now()): void {
  try {
    const unfinished = jobs
      .filter((job) => UNFINISHED.includes(job.status))
      .map(({ items: _items, uploaded: _uploaded, ...rest }) => ({ ...rest, items: [], uploaded: {} }))
    if (unfinished.length) window.localStorage.setItem(OWN_KEY, JSON.stringify({ seen, jobs: unfinished }))
    else window.localStorage.removeItem(OWN_KEY)
    for (const key of adoptedKeys) window.localStorage.removeItem(key)
    adoptedKeys.clear()
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
  const [leaving, setLeaving] = useState(false)
  // Jobs whose file lists are folded open in the panel.
  const [openFiles, setOpenFiles] = useState<Set<string>>(() => new Set())
  const leaveTimer = useRef(0)
  const jobsRef = useRef(jobs)
  const controllers = useRef(new Map<string, AbortController>())
  const running = useRef(false)
  const chunkBytes = useRef(8 * 1024 * 1024)
  const confirm = useConfirm()

  useEffect(() => {
    jobsRef.current = jobs
    persistJobs(jobs)
  }, [jobs])

  const owning = jobs.some((job) => UNFINISHED.includes(job.status))
  useEffect(() => {
    if (!owning) return
    const renew = () => persistJobs(jobsRef.current)
    // A closed or reloaded tab lets go at once, so the next page load can pick
    // its jobs up as interrupted; a page restored from the back cache takes them back.
    const release = () => persistJobs(jobsRef.current, 0)
    const timer = window.setInterval(renew, LEASE_HEARTBEAT_MS)
    window.addEventListener('pagehide', release)
    window.addEventListener('pageshow', renew)
    return () => {
      window.clearInterval(timer)
      window.removeEventListener('pagehide', release)
      window.removeEventListener('pageshow', renew)
    }
  }, [owning])

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

  const active = jobs.some((job) => IN_PROGRESS.includes(job.status))

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
          const started = (await api.startChange(job.repoId)).id
          // Cancelled while the session was being opened: close it again.
          if (controller.signal.aborted) {
            await api.abortChange(started).catch(() => undefined)
            throw new DOMException('Upload cancelled', 'AbortError')
          }
          sessionId = started
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
          const destination: UploadDestination =
            job.kind === 'change' && sessionId ? { sessionId } : { repoId: job.repoId }
          // Storage that takes uploads straight from the browser gets the parts;
          // otherwise the file goes to the server in chunks.
          const begun = await api.beginDirectFile(destination, item.path, item.file.size, controller.signal)
          if (begun.direct) {
            await uploadDirect(
              item.file,
              begun,
              {
                parts: (numbers) => api.directFileParts(destination, item.path, numbers, controller.signal),
                complete: () => api.completeDirectFile(destination, item.path, controller.signal),
              },
              progress,
              controller.signal,
            )
          } else if (job.kind === 'change' && sessionId) {
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
          // The staged files of a change are discarded with its session.
          if (job.kind === 'change' && sessionId) await api.abortChange(sessionId).catch(() => undefined)
          update(job.id, { status: 'cancelled', currentFile: undefined, sessionId: undefined })
        } else {
          const message = reason instanceof Error ? reason.message : 'The upload stopped.'
          update(job.id, { status: 'error', error: message, currentFile: undefined })
          const sentence = /[.!?]$/.test(message) ? message : `${message}.`
          onToast(`${job.repoId}: ${sentence} Progress is saved; retry to resume.`, 'error')
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

  /** Resolves true once the job is cancelled. A change's staged files are
   * thrown away with its session, so that asks first; a new repository keeps
   * what arrived and can resume. */
  async function cancel(job: UploadJob): Promise<boolean> {
    if (job.kind === 'change' && (job.sessionId || uploadedBytes(job) > 0)) {
      const sure = await confirm({
        eyebrow: 'Cancel upload',
        title: `Discard the change to ${job.repoId}?`,
        message: `The files already sent for “${job.message}” are thrown away and the repository stays as it was. To make this change later, upload the files again.`,
        confirmLabel: 'Discard change',
        danger: true,
      })
      if (!sure) return false
      // It may have finished while the question was open.
      const latest = jobsRef.current.find((entry) => entry.id === job.id)
      if (!latest || latest.status === 'done') return false
      job = latest
    }
    const controller = controllers.current.get(job.id)
    if (controller) {
      // A running job closes its own change session, including one still opening.
      controller.abort()
      return true
    }
    if (job.kind === 'change' && job.sessionId) {
      await api.abortChange(job.sessionId).catch(() => undefined)
    }
    update(job.id, { status: 'cancelled', sessionId: undefined })
    return true
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

  useEffect(() => () => window.clearTimeout(leaveTimer.current), [])

  // The page keeps room at its foot for the panel (see `--upload-dock-space` in
  // styles.css), so whatever it floats over can still be scrolled clear of it.
  const dock = useRef<HTMLElement>(null)
  const docked = jobs.length > 0
  useEffect(() => {
    const element = dock.current
    const root = document.documentElement
    if (!docked || !element) return
    const measure = () => {
      const bottom = parseFloat(getComputedStyle(element).bottom) || 0
      root.style.setProperty('--upload-dock-space', `${Math.ceil(element.offsetHeight + bottom)}px`)
    }
    measure()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
    observer?.observe(element)
    window.addEventListener('resize', measure)
    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', measure)
      root.style.removeProperty('--upload-dock-space')
    }
  }, [docked])

  function clearFinished() {
    const finished = new Set(
      jobs.filter((job) => !['interrupted', 'error'].includes(job.status)).map((job) => job.id),
    )
    const drop = () => setJobs((all) => all.filter((job) => !finished.has(job.id)))
    // The panel leaves as one piece when nothing would be left in it.
    if (finished.size < jobs.length) {
      drop()
      return
    }
    setLeaving(true)
    window.clearTimeout(leaveTimer.current)
    leaveTimer.current = window.setTimeout(() => {
      drop()
      setLeaving(false)
    }, DOCK_EXIT_MS)
  }

  const value = useMemo(() => ({ jobs, active, enqueue }), [jobs, active, enqueue])
  const visible = jobs
  const current = jobs.find((job) => job.status === 'uploading' || job.status === 'committing')
  // Paused, failed, and interrupted jobs are not moving, so they leave the total.
  const totals = jobs
    .filter((job) => IN_PROGRESS.includes(job.status))
    .reduce(
      (sum, job) => ({ done: sum.done + uploadedBytes(job), total: sum.total + job.total }),
      { done: 0, total: 0 },
    )
  const percent = totals.total ? Math.min(100, (totals.done / totals.total) * 100) : 100
  const label = useFadeOnChange<HTMLSpanElement>(active ? 'active' : 'idle')
  // Collapsed parts stay in the page so they can fold smoothly, but out of reach.
  const inert = (hidden: boolean) => (element: HTMLElement | null) => {
    if (hidden) element?.setAttribute('inert', '')
    else element?.removeAttribute('inert')
  }

  return (
    <UploadContext.Provider value={value}>
      {children}
      {visible.length > 0 && (
        <aside
          ref={dock}
          className={['upload-dock', minimized && 'minimized', active && 'live', leaving && !active && 'leaving']
            .filter(Boolean)
            .join(' ')}
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
              <span ref={label}>
                {active
                  ? `Uploading${current ? ` ${current.repoId}` : ''} · ${percent.toFixed(0)}%`
                  : `${visible.length} upload${visible.length === 1 ? '' : 's'}`}
              </span>
              <ChevronDown size={15} className="upload-dock-chevron" />
            </button>
            {!active && (
              <button
                type="button"
                className="icon-button"
                aria-label="Clear finished uploads"
                title="Clear finished uploads"
                onClick={clearFinished}
              >
                <X size={16} />
              </button>
            )}
          </header>
          <div className="upload-dock-fold upload-dock-mini" ref={inert(true)} aria-hidden="true">
            <div>
              <div className={active ? 'job-progress live upload-dock-mini-progress' : 'job-progress upload-dock-mini-progress'}>
                <span style={{ width: `${percent}%` }} />
              </div>
            </div>
          </div>
          <div className="upload-dock-fold upload-dock-body" ref={inert(minimized)} aria-hidden={minimized || undefined}>
            <div>
              <ul className="upload-dock-list">
                {visible.map((job) => {
                  const done = uploadedBytes(job)
                  const jobPercent = job.total ? Math.min(100, (done / job.total) * 100) : 0
                  // Files named with a leading dot are stored but not listed in the commit.
                  const committed = job.items.filter((item) => isRecorded(item.path)).length
                  return (
                    <li key={job.id} className={`upload-job ${job.status}`}>
                      <div className="upload-job-title">
                        <Link to={`/models/${job.repoId}`}>{job.repoId}</Link>
                        <span>{job.message}</span>
                      </div>
                      {['uploading', 'queued', 'committing'].includes(job.status) && (
                        <>
                          <div className={job.status === 'queued' ? 'job-progress' : 'job-progress live'}>
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
                          <Check size={13} /> Committed {committed} file
                          {committed === 1 ? '' : 's'}
                          {job.deletions.length ? `, deleted ${job.deletions.length}` : ''}
                        </p>
                      )}
                      {job.status === 'cancelled' && (
                        <p className="upload-job-state">
                          {job.kind === 'change'
                            ? 'Cancelled; staged files were discarded.'
                            : 'Cancelled. Files already sent are kept: resume or delete the repository under Unfinished uploads.'}
                        </p>
                      )}
                      {job.error && (
                        <p className="upload-job-state danger">
                          <AlertCircle size={13} /> {job.error}
                        </p>
                      )}
                      {job.status === 'interrupted' && (
                        <p className="upload-job-state">
                          This upload&apos;s tab was closed or reloaded. Choose the same folder again to resume where it stopped.
                        </p>
                      )}
                      <div className="upload-job-actions">
                        {job.expected.length + job.deletions.length > 0 && (
                          <button
                            type="button"
                            className="upload-job-files-toggle"
                            aria-expanded={openFiles.has(job.id)}
                            aria-controls={`upload-files-${job.id}`}
                            onClick={() =>
                              setOpenFiles((current) => {
                                const next = new Set(current)
                                if (!next.delete(job.id)) next.add(job.id)
                                return next
                              })
                            }
                          >
                            <ChevronDown size={13} />
                            {job.expected.length} file{job.expected.length === 1 ? '' : 's'}
                            {job.deletions.length ? `, ${job.deletions.length} deleted` : ''}
                          </button>
                        )}
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
                          <button type="button" onClick={() => cancel(job).then((cancelled) => cancelled && setJobs((all) => all.filter((entry) => entry.id !== job.id)))}>
                            Discard
                          </button>
                        )}
                      </div>
                      <JobFiles job={job} open={openFiles.has(job.id)} id={`upload-files-${job.id}`} />
                    </li>
                  )
                })}
              </ul>
            </div>
          </div>
        </aside>
      )}
    </UploadContext.Provider>
  )
}
