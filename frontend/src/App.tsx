import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertCircle,
  ArrowDownToLine,
  Ban,
  Box,
  Check,
  ChevronDown,
  CircleX,
  Clock3,
  Filter,
  ListFilter,
  LoaderCircle,
  PackageCheck,
  RefreshCw,
  Search,
  SlidersHorizontal,
} from 'lucide-react'
import {
  HashRouter,
  Navigate,
  Route,
  Routes,
  useNavigate,
  useSearchParams,
} from 'react-router-dom'
import { api } from './api'
import { AuthScreen, SavedPage, UploadsPage } from './components/AccountPages'
import { LibraryModelRow } from './components/RepositoryRows'
import { AccessProvider, useAccess } from './access'
import { AccountPage } from './pages/AccountPage'
import { AdminPage } from './pages/AdminPage'
import { ModelPage } from './pages/ModelPage'
import { UploadProvider } from './uploads'
import Shell from './components/Shell'
import type {
  AuthStatus,
  DownloadJob,
  LibraryFacets,
  LibraryModel,
  User,
} from './types'
import { formatBytes, relativeTime } from './utils'

type ToastTone = 'success' | 'error'
type ToastHandler = (message: string, tone?: ToastTone) => void

const parameterOptions = [
  ['max:1B', '< 1B'],
  ['min:1B,max:7B', '1B – 7B'],
  ['min:7B,max:32B', '7B – 32B'],
  ['min:32B,max:128B', '32B – 128B'],
  ['min:128B', '> 128B'],
]

function FilterGroup({
  title,
  options,
  value,
  onChange,
}: {
  title: string
  options: string[][]
  value: string
  onChange: (value: string) => void
}) {
  return (
    <section className="filter-group">
      <h3>{title}</h3>
      {options.map(([id, label]) => (
        <button
          type="button"
          key={id}
          className={value === id ? 'selected' : ''}
          onClick={() => onChange(value === id ? '' : id)}
        >
          <span className="filter-check">{value === id && <Check size={12} />}</span>
          {label}
        </button>
      ))}
    </section>
  )
}

function ModelsPage({ onToast }: { onToast: ToastHandler }) {
  const [searchParams, setSearchParams] = useSearchParams()
  const [search, setSearch] = useState(searchParams.get('search') || '')
  const [task, setTask] = useState('')
  const [library, setLibrary] = useState('')
  const [appFilter, setAppFilter] = useState('')
  const [parameters, setParameters] = useState('')
  const { user, can } = useAccess()
  const [sort, setSort] = useState<string>(user.preferences?.catalog_sort || 'updated')
  const [models, setModels] = useState<LibraryModel[]>([])
  const [facets, setFacets] = useState<LibraryFacets>({ tasks: [], libraries: [], apps: [] })
  const [libraryTotal, setLibraryTotal] = useState(0)
  const [libraryBytes, setLibraryBytes] = useState(0)
  const [loading, setLoading] = useState(true)
  const [scanning, setScanning] = useState(false)
  const [error, setError] = useState('')
  const [mobileFiltersOpen, setMobileFiltersOpen] = useState(false)
  const [saving, setSaving] = useState<string | null>(null)
  const navigate = useNavigate()
  const urlSearch = searchParams.get('search') || ''
  const legacyModel = searchParams.get('model')

  useEffect(() => {
    setSearch(urlSearch)
  }, [urlSearch])

  // Older links opened a drawer: #/models?model=owner/name&local-app=vllm or &clone=true.
  useEffect(() => {
    if (!legacyModel) return
    const next = new URLSearchParams()
    if (searchParams.get('local-app')) next.set('local-app', searchParams.get('local-app') || '')
    if (searchParams.get('clone')) next.set('clone', searchParams.get('clone') || '')
    const query = next.toString()
    navigate(`/models/${legacyModel}${query ? `?${query}` : ''}`, { replace: true })
  }, [legacyModel, navigate, searchParams])

  const fetchModels = useCallback(() => {
    const params = new URLSearchParams({ search, sort })
    if (task) params.set('task', task)
    if (library) params.set('library', library)
    if (appFilter) params.set('app', appFilter)
    if (parameters) params.set('parameters', parameters)
    setLoading(true)
    setError('')
    api
      .libraryModels(params)
      .then((payload) => {
        setModels(payload.items)
        setFacets(payload.facets)
        setLibraryTotal(payload.total)
        setLibraryBytes(payload.total_bytes)
      })
      .catch((reason) => setError(reason.message))
      .finally(() => setLoading(false))
  }, [appFilter, library, parameters, search, sort, task])

  useEffect(() => {
    const timer = window.setTimeout(fetchModels, 250)
    return () => window.clearTimeout(timer)
  }, [fetchModels])

  function submitSearch() {
    const next = new URLSearchParams(searchParams)
    if (search.trim()) next.set('search', search.trim())
    else next.delete('search')
    setSearchParams(next)
  }

  async function rescan() {
    setScanning(true)
    try {
      const result = await api.scanLocalModels()
      onToast(`Indexed ${result.count} model${result.count === 1 ? '' : 's'}.`)
      fetchModels()
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Library scan failed', 'error')
    } finally {
      setScanning(false)
    }
  }

  const activeFilters = [task, library, appFilter, parameters].filter(Boolean).length

  async function toggleSaved(model: LibraryModel) {
    setSaving(model.id)
    try {
      if (model.saved) {
        await api.unsaveModel(model.id)
        onToast(`${model.id} was removed from your saved library.`)
      } else {
        await api.saveModel({
          repo_id: model.id,
          metadata: {
            author: model.author,
            pipeline_tag: model.pipeline_tag,
            library_name: model.library_name,
            license: model.license,
            parameter_count: model.parameter_count,
            last_modified: model.last_modified,
            local: true,
          },
        })
        onToast(`${model.id} was saved for later.`)
      }
      setModels((current) =>
        current.map((item) =>
          item.id === model.id ? { ...item, saved: !model.saved } : item,
        ),
      )
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Unable to update saved models', 'error')
    } finally {
      setSaving(null)
    }
  }

  return (
    <>
      <div className="catalog-layout">
        <aside className={mobileFiltersOpen ? 'filters mobile-open' : 'filters'}>
          <div className="filters-heading">
            <Filter size={16} />
            <span>Models</span>
            {activeFilters > 0 && <em>{activeFilters}</em>}
            <button
              type="button"
              className="filter-mobile-close"
              onClick={() => setMobileFiltersOpen(false)}
              aria-label="Close filters"
            >
              <CircleX size={18} />
            </button>
          </div>
          {facets.tasks.length > 0 && (
            <FilterGroup title="Tasks" options={facets.tasks} value={task} onChange={setTask} />
          )}
          {facets.libraries.length > 0 && (
            <FilterGroup
              title="Libraries & formats"
              options={facets.libraries}
              value={library}
              onChange={setLibrary}
            />
          )}
          {facets.apps.length > 0 && (
            <FilterGroup title="Runs with" options={facets.apps} value={appFilter} onChange={setAppFilter} />
          )}
          <FilterGroup
            title="Parameters"
            options={parameterOptions}
            value={parameters}
            onChange={setParameters}
          />
          {activeFilters > 0 && (
            <button
              className="clear-filters"
              onClick={() => {
                setTask('')
                setLibrary('')
                setAppFilter('')
                setParameters('')
              }}
            >
              Reset filters
            </button>
          )}
        </aside>

        <section className="catalog-content">
          <div className="page-heading catalog-heading">
            <div>
              <span className="eyebrow">Your offline model library</span>
              <h1>Explore models</h1>
              <p>Everything here is served from your own storage. No internet connection required.</p>
            </div>
            {can('library.scan') && (
              <button type="button" className="quiet-link" onClick={rescan} disabled={scanning}>
                <RefreshCw size={14} className={scanning ? 'spin' : undefined} />
                {scanning ? 'Scanning…' : 'Rescan library'}
              </button>
            )}
          </div>

          <div className="catalog-tools">
            <div className="catalog-search">
              <Search size={18} />
              <input
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') submitSearch()
                }}
                placeholder="Search model names, owners, tasks, and tags"
              />
              {search && (
                <button type="button" onClick={() => setSearch('')} aria-label="Clear search">
                  <CircleX size={16} />
                </button>
              )}
            </div>
            <label className="sort-control">
              <SlidersHorizontal size={15} />
              <select value={sort} onChange={(event) => setSort(event.target.value)} aria-label="Sort models">
                <option value="updated">Recently updated</option>
                <option value="name">Name</option>
                <option value="size">Largest on disk</option>
                <option value="parameters">Most parameters</option>
              </select>
              <ChevronDown size={14} />
            </label>
            <button
              type="button"
              className="secondary-button mobile-filter-button"
              onClick={() => setMobileFiltersOpen(true)}
            >
              <ListFilter size={15} />
              Filters {activeFilters > 0 ? `(${activeFilters})` : ''}
            </button>
          </div>

          <div className="results-line">
            <span>
              {loading
                ? 'Reading the local library…'
                : models.length === libraryTotal
                  ? `${models.length} models`
                  : `${models.length} of ${libraryTotal} models shown`}
            </span>
            <span>{formatBytes(libraryBytes)} stored locally</span>
          </div>

          {error && (
            <div className="page-error">
              <AlertCircle size={18} />
              <div>
                <strong>Could not read the local library</strong>
                <p>{error}</p>
              </div>
              <button onClick={fetchModels}>Retry</button>
            </div>
          )}
          {loading ? (
            <div className="model-card-grid skeleton-card-grid" aria-label="Loading models">
              {Array.from({ length: 8 }).map((_, index) => (
                <div key={index} className="model-card skeleton-card">
                  <div className="skeleton skeleton-visual" />
                  <div className="model-card-body">
                    <div className="skeleton line-short" />
                    <div className="skeleton line-strong" />
                    <div className="skeleton line-medium" />
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="model-card-grid">
              {models.map((model) => (
                <LibraryModelRow
                  key={model.id}
                  model={model}
                  onOpen={(repoId) => navigate(`/models/${repoId}`)}
                  onUse={(item) =>
                    navigate(`/models/${item.id}?${item.apps.includes('vllm') ? 'local-app=vllm' : 'clone=true'}`)
                  }
                  onSave={toggleSaved}
                  saving={saving === model.id}
                />
              ))}
              {!error && models.length === 0 && (
                <div className="empty-state">
                  <Box size={30} />
                  {libraryTotal === 0 ? (
                    <>
                      <h2>Your library is empty</h2>
                      <p>
                        Copy model folders into the models storage as owner/model-name, then
                        rescan. You can also add models from the Uploads page.
                      </p>
                    </>
                  ) : (
                    <>
                      <h2>No matching models</h2>
                      <p>Clear a filter or try a broader repository name.</p>
                    </>
                  )}
                </div>
              )}
            </div>
          )}
        </section>
      </div>
    </>
  )
}

const activeDownloadStatuses = ['queued', 'preparing', 'downloading']

const downloadModeLabels = {
  full: 'Full repository',
  safetensors: 'SafeTensors',
  gguf: 'GGUF selection',
  metadata: 'Metadata only',
  custom: 'Custom selection',
}

function DownloadStatus({
  job,
  onCancel,
  cancelling,
}: {
  job: DownloadJob
  onCancel?: (job: DownloadJob) => void
  cancelling?: boolean
}) {
  const isActive = activeDownloadStatuses.includes(job.status)
  const remaining = Math.max(0, job.total_bytes - job.downloaded_bytes)
  const eta = job.speed_bps > 0 ? Math.round(remaining / job.speed_bps) : 0
  const etaLabel = eta
    ? eta > 3600
      ? `${Math.round(eta / 3600)}h remaining`
      : eta > 60
        ? `${Math.round(eta / 60)}m remaining`
        : `${eta}s remaining`
    : ''
  const modeLabel = downloadModeLabels[job.payload.mode || 'full']

  return (
    <article className="download-row">
      <div className={`download-state-icon ${job.status}`}>
        {job.status === 'complete' ? (
          <PackageCheck size={19} />
        ) : job.status === 'failed' ? (
          <AlertCircle size={18} />
        ) : job.status === 'cancelled' ? (
          <Ban size={18} />
        ) : (
          <ArrowDownToLine size={18} />
        )}
      </div>
      <div className="download-main">
        <div className="download-title">
          <div>
            <h3>{job.repo_id}</h3>
            <span>{modeLabel} · revision {job.revision}</span>
          </div>
          <div className="download-title-actions">
            <strong className={`job-status ${job.status}`}>{job.status}</strong>
            {isActive && onCancel && (
              <button
                type="button"
                className="cancel-download-button"
                onClick={() => onCancel(job)}
                disabled={cancelling}
              >
                {cancelling ? <LoaderCircle size={14} className="spin" /> : <Ban size={14} />}
                {cancelling ? 'Cancelling…' : 'Cancel'}
              </button>
            )}
          </div>
        </div>
        {isActive && (
          <>
            <div className="job-progress" aria-label={`${job.progress.toFixed(0)} percent downloaded`}>
              <span style={{ width: `${Math.max(job.progress, job.status === 'preparing' ? 2 : 0)}%` }} />
            </div>
            <div className="download-stats">
              <span>
                {formatBytes(job.downloaded_bytes)}
                {job.total_bytes > 0 && ` of ${formatBytes(job.total_bytes)}`}
              </span>
              <span>{job.speed_bps > 0 ? `${formatBytes(job.speed_bps)}/s` : 'Preparing repository…'}</span>
              {etaLabel && <span>{etaLabel}</span>}
              {typeof job.metadata.file_count === 'number' && <span>{job.metadata.file_count} repository files</span>}
            </div>
          </>
        )}
        {job.status === 'complete' && (
          <div className="download-stats">
            <span>{formatBytes(job.downloaded_bytes)} stored</span>
            <span>Completed {relativeTime(job.completed_at)}</span>
            <code>{job.target_path}</code>
          </div>
        )}
        {job.status === 'cancelled' && (
          <div className="download-stats cancelled-copy">
            <span>{formatBytes(job.downloaded_bytes)} retained</span>
            <span>Partial files stay in place so a future download can resume.</span>
          </div>
        )}
        {job.status === 'failed' && (
          <div className="download-error">
            <AlertCircle size={15} />
            <span>{job.error}</span>
          </div>
        )}
      </div>
    </article>
  )
}

function DownloadsPage({
  jobs,
  onToast,
  refreshDownloads,
}: {
  jobs: DownloadJob[]
  onToast: ToastHandler
  refreshDownloads: () => void
}) {
  const [cancelling, setCancelling] = useState<string | null>(null)
  const active = jobs.filter((job) => activeDownloadStatuses.includes(job.status))
  const finished = jobs.filter((job) => !active.includes(job))
  const totalSpeed = active.reduce((total, job) => total + job.speed_bps, 0)
  const remainingBytes = active.reduce(
    (total, job) => total + Math.max(0, job.total_bytes - job.downloaded_bytes),
    0,
  )
  const completedCount = jobs.filter((job) => job.status === 'complete').length

  async function cancel(job: DownloadJob) {
    setCancelling(job.id)
    try {
      await api.cancelDownload(job.id)
      onToast(`${job.repo_id} was cancelled. Partial files were kept for resume.`)
      refreshDownloads()
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Unable to cancel download', 'error')
    } finally {
      setCancelling(null)
    }
  }

  return (
    <div className="standard-page downloads-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">Persistent background transfers</span>
          <h1>Downloads</h1>
          <p>Monitor precise transfer activity, stop work safely, and keep partial files ready to resume.</p>
        </div>
      </div>

      <section className="transfer-overview" aria-label="Download activity summary">
        <div><span>Active transfers</span><strong>{active.length}</strong><small>queued or downloading</small></div>
        <div><span>Combined speed</span><strong>{totalSpeed > 0 ? `${formatBytes(totalSpeed)}/s` : '—'}</strong><small>across active jobs</small></div>
        <div><span>Remaining</span><strong>{remainingBytes > 0 ? formatBytes(remainingBytes) : '—'}</strong><small>known repository data</small></div>
        <div><span>Completed</span><strong>{completedCount}</strong><small>stored in the library</small></div>
      </section>

      <section className="download-section">
        <h2>Active</h2>
        <div className="download-list">
          {active.map((job) => (
            <DownloadStatus
              key={job.id}
              job={job}
              onCancel={cancel}
              cancelling={cancelling === job.id}
            />
          ))}
          {active.length === 0 && (
            <div className="empty-compact">
              <Clock3 size={18} /> No active downloads.
            </div>
          )}
        </div>
      </section>
      <section className="download-section">
        <h2>History</h2>
        <div className="download-list">
          {finished.map((job) => (
            <DownloadStatus key={job.id} job={job} />
          ))}
          {finished.length === 0 && <div className="empty-compact">Completed, cancelled, and failed jobs appear here.</div>}
        </div>
      </section>
    </div>
  )
}

function Application({
  authStatus,
  onAuthChange,
}: {
  authStatus: AuthStatus
  onAuthChange: (status: AuthStatus) => void
}) {
  const [jobs, setJobs] = useState<DownloadJob[]>([])
  const [toast, setToast] = useState<{ message: string; tone: ToastTone } | null>(null)

  const showToast = useCallback((message: string, tone: ToastTone = 'success') => {
    setToast({ message, tone })
  }, [])

  const capabilities = authStatus.capabilities || []
  const can = useCallback((capability: string) => capabilities.includes(capability), [capabilities])
  const canDownload = can('hub.download')

  const refreshDownloads = useCallback(() => {
    if (!canDownload) return
    api
      .downloads()
      .then((payload) => setJobs(payload.items))
      .catch(() => undefined)
  }, [canDownload])

  useEffect(() => {
    if (!canDownload) return
    refreshDownloads()
    const timer = window.setInterval(refreshDownloads, 1800)
    return () => window.clearInterval(timer)
  }, [canDownload, refreshDownloads])

  const refreshAccess = useCallback(() => {
    api.authStatus().then(onAuthChange).catch(() => undefined)
  }, [onAuthChange])

  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => setToast(null), 4500)
    return () => window.clearTimeout(timer)
  }, [toast])

  const activeDownloads = useMemo(
    () => jobs.filter((job) => ['queued', 'preparing', 'downloading'].includes(job.status)).length,
    [jobs],
  )

  const user = authStatus.user as User

  async function logout() {
    try {
      await api.logout()
    } finally {
      onAuthChange({ ...authStatus, user: null, csrf_token: null })
    }
  }

  return (
    <AccessProvider user={user} capabilities={capabilities} refresh={refreshAccess}>
    <UploadProvider onToast={showToast}>
    <Shell activeDownloads={activeDownloads} user={user} onLogout={logout}>
      <Routes>
        <Route path="/" element={<Navigate to="/models" replace />} />
        <Route
          path="/models"
          element={<ModelsPage onToast={showToast} />}
        />
        <Route
          path="/models/:owner/:name/*"
          element={<ModelPage onToast={showToast} />}
        />
        <Route path="/local" element={<Navigate to={can('storage.view') ? '/admin/storage' : '/models'} replace />} />
        <Route path="/storage" element={<Navigate to="/admin/storage" replace />} />
        <Route path="/runtimes" element={<Navigate to="/admin/runtimes" replace />} />
        <Route path="/settings" element={<Navigate to={can('settings.view') ? '/admin/server' : '/account'} replace />} />
        <Route path="/saved" element={<SavedPage onToast={showToast} />} />
        <Route
          path="/uploads"
          element={can('repos.create') ? <UploadsPage user={user} onToast={showToast} /> : <Navigate to="/models" replace />}
        />
        <Route
          path="/downloads"
          element={
            canDownload ? (
              <DownloadsPage
                jobs={jobs}
                onToast={showToast}
                refreshDownloads={refreshDownloads}
              />
            ) : (
              <Navigate to="/models" replace />
            )
          }
        />
        <Route path="/account" element={<AccountPage onToast={showToast} />} />
        <Route path="/account/:tab" element={<AccountPage onToast={showToast} />} />
        <Route path="/admin" element={<AdminPage onToast={showToast} />} />
        <Route path="/admin/:tab" element={<AdminPage onToast={showToast} />} />
        <Route path="*" element={<Navigate to="/models" replace />} />
      </Routes>
      {toast && (
        <div className={`toast ${toast.tone}`} role="status">
          {toast.tone === 'error' ? <AlertCircle size={16} /> : <Check size={16} />}
          {toast.message}
        </div>
      )}
    </Shell>
    </UploadProvider>
    </AccessProvider>
  )
}

export default function App() {
  const [status, setStatus] = useState<AuthStatus | null>(null)
  const [error, setError] = useState('')

  const refreshAuth = useCallback(() => {
    setError('')
    api
      .authStatus()
      .then(setStatus)
      .catch((reason) => setError(reason instanceof Error ? reason.message : 'Unable to reach HuggingHack'))
  }, [])

  useEffect(() => {
    refreshAuth()
    window.addEventListener('hugginghack:unauthorized', refreshAuth)
    return () => window.removeEventListener('hugginghack:unauthorized', refreshAuth)
  }, [refreshAuth])

  if (error) {
    return (
      <main className="auth-layout auth-unavailable">
        <section className="auth-card">
          <AlertCircle size={28} />
          <h1>HuggingHack is unavailable</h1>
          <p>{error}</p>
          <button className="secondary-button" onClick={refreshAuth}><RefreshCw size={16} /> Retry</button>
        </section>
      </main>
    )
  }

  if (!status) {
    return (
      <main className="app-loading">
        <img src="/hugginghack-mark.svg" alt="" />
        <LoaderCircle size={23} className="spin" />
        <span>Opening your model library…</span>
      </main>
    )
  }

  if (status.setup_required || !status.user) {
    return <AuthScreen setup={status.setup_required} onAuthenticated={setStatus} />
  }

  return (
    <HashRouter>
      <Application authStatus={status} onAuthChange={setStatus} />
    </HashRouter>
  )
}
