import { useCallback, useEffect, useRef, useState } from 'react'
import {
  AlertCircle,
  Box,
  Check,
  ChevronDown,
  CircleX,
  Filter,
  ListFilter,
  LoaderCircle,
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
  useLocation,
  useSearchParams,
} from 'react-router-dom'
import { api } from './api'
import { AuthScreen, SavedPage, UploadsPage } from './components/AccountPages'
import { EMPTY_FILTERS, ModelFilters, activeFilterCount, applyFilters, type ModelFilterState } from './components/ModelFilters'
import { LibraryModelRow } from './components/RepositoryRows'
import { AccessProvider, useAccess } from './access'
import { AccountPage } from './pages/AccountPage'
import { AdminPage } from './pages/AdminPage'
import { ModelPage } from './pages/ModelPage'
import { OrganizationPage, OrganizationsIndex } from './pages/OrganizationPage'
import { TOAST_EXIT_MS, useFadeOnChange } from './motion'
import { UploadProvider } from './uploads'
import Shell from './components/Shell'
import type {
  AuthStatus,
  LibraryFacets,
  LibraryModel,
  User,
} from './types'
import { formatBytes } from './utils'

type ToastTone = 'success' | 'error'
type ToastHandler = (message: string, tone?: ToastTone) => void

function ModelsPage({ onToast }: { onToast: ToastHandler }) {
  const [searchParams, setSearchParams] = useSearchParams()
  const [search, setSearch] = useState(searchParams.get('search') || '')
  const [filters, setFilters] = useState<ModelFilterState>(EMPTY_FILTERS)
  const { user, can } = useAccess()
  const [sort, setSort] = useState<string>(user.preferences?.catalog_sort || 'updated')
  const [models, setModels] = useState<LibraryModel[]>([])
  const [facets, setFacets] = useState<LibraryFacets>({ tasks: {}, precision: {}, hardware: [] })
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
    const params = applyFilters(filters, new URLSearchParams({ search, sort }))
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
  }, [filters, search, sort])

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

  const activeFilters = activeFilterCount(filters)
  const hardwareLabels = Object.fromEntries(facets.hardware.map(([id, label]) => [id, label]))

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
          <ModelFilters facets={facets} value={filters} onChange={setFilters} />
          {activeFilters > 0 && (
            <button
              className="clear-filters"
              onClick={() => setFilters(EMPTY_FILTERS)}
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
                  hardwareLabels={hardwareLabels}
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

function Application({
  authStatus,
  onAuthChange,
}: {
  authStatus: AuthStatus
  onAuthChange: (status: AuthStatus) => void
}) {
  const [toast, setToast] = useState<{ id: number; message: string; tone: ToastTone } | null>(null)
  const [leavingToast, setLeavingToast] = useState(0)
  const toastIds = useRef(0)
  const location = useLocation()
  // One fade per page, not per tab or filter: tabbed pages fade their own body.
  const segments = location.pathname.split('/')
  const view = useFadeOnChange<HTMLDivElement>(segments.slice(0, segments[1] === 'models' ? 4 : 2).join('/'), { initial: true })

  const showToast = useCallback((message: string, tone: ToastTone = 'success') => {
    setToast({ id: ++toastIds.current, message, tone })
  }, [])

  const capabilities = authStatus.capabilities || []
  const can = useCallback((capability: string) => capabilities.includes(capability), [capabilities])

  const refreshAccess = useCallback(() => {
    api.authStatus().then(onAuthChange).catch(() => undefined)
  }, [onAuthChange])

  useEffect(() => {
    if (!toast) return
    const leave = window.setTimeout(() => setLeavingToast(toast.id), 4500)
    const clear = window.setTimeout(() => setToast(null), 4500 + TOAST_EXIT_MS)
    return () => {
      window.clearTimeout(leave)
      window.clearTimeout(clear)
    }
  }, [toast])

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
    <Shell user={user} onLogout={logout}>
      <div ref={view}>
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
        <Route path="/downloads" element={<Navigate to="/models" replace />} />
        <Route path="/orgs" element={<OrganizationsIndex />} />
        <Route path="/orgs/:name" element={<OrganizationPage onToast={showToast} />} />
        <Route path="/orgs/:name/:tab" element={<OrganizationPage onToast={showToast} />} />
        <Route path="/account" element={<AccountPage onToast={showToast} />} />
        <Route path="/account/:tab" element={<AccountPage onToast={showToast} />} />
        <Route path="/admin" element={<AdminPage onToast={showToast} />} />
        <Route path="/admin/:tab" element={<AdminPage onToast={showToast} />} />
        <Route path="*" element={<Navigate to="/models" replace />} />
      </Routes>
      </div>
      {toast && (
        <div key={toast.id} className={`toast ${toast.tone}${leavingToast === toast.id ? ' leaving' : ''}`} role="status">
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
    return <AuthScreen setup={status.setup_required} oidc={status.oidc} onAuthenticated={setStatus} />
  }

  return (
    <HashRouter>
      <Application authStatus={status} onAuthChange={setStatus} />
    </HashRouter>
  )
}
