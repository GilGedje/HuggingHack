import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertCircle,
  Box,
  Check,
  ChevronDown,
  CircleX,
  Filter,
  GitFork,
  ListFilter,
  LoaderCircle,
  PanelLeftClose,
  PanelLeftOpen,
  RefreshCw,
  Search,
  SlidersHorizontal,
} from 'lucide-react'
import {
  HashRouter,
  Link,
  Navigate,
  Route,
  Routes,
  useNavigate,
  useLocation,
  useSearchParams,
} from 'react-router-dom'
import { api } from './api'
import { AuthScreen, SavedPage } from './components/AccountPages'
import { UploadsPage } from './pages/UploadsPage'
import { EMPTY_FILTERS, ModelFilters, activeFilterCount, applyFilters, type ModelFilterState } from './components/ModelFilters'
import { LibraryModelRow } from './components/RepositoryRows'
import { ModelCardSkeletons } from './components/Skeletons'
import { LoadError } from './components/LoadError'
import { AccessProvider, useAccess } from './access'
import { AccountPage } from './pages/AccountPage'
import { AdminPage } from './pages/AdminPage'
import { ModelPage } from './pages/ModelPage'
import { OrganizationPage, OrganizationsIndex } from './pages/OrganizationPage'
import { TOAST_EXIT_MS, crossfade, useFadeOnChange } from './motion'
import { useAppTheme } from './appTheme'
import { brandMark, type Theme } from './theme'
import { UploadProvider } from './uploads'
import Shell from './components/Shell'
import type {
  AuthStatus,
  LibraryFacets,
  LibraryModel,
  User,
} from './types'
import { formatBytes } from './utils'
import { ConfirmProvider } from './components/ConfirmDialog'
import { relationGroup } from './modelTree'
import { CATALOG_FILTER_KEYS, readCatalogFilters, writeCatalogFilters, writeCatalogSearch } from './catalog'

type ToastTone = 'success' | 'error'

// Whether the Explore filters are tucked away is a per-browser convenience.
const FILTERS_HIDDEN_KEY = 'hugginghack.explore.filtersHidden'

function readFiltersHidden(): boolean {
  try {
    return localStorage.getItem(FILTERS_HIDDEN_KEY) === '1'
  } catch {
    return false
  }
}
type ToastHandler = (message: string, tone?: ToastTone) => void

const SORTS = ['updated', 'name', 'size', 'parameters']

function ModelsPage({ onToast }: { onToast: ToastHandler }) {
  const [searchParams, setSearchParams] = useSearchParams()
  const [search, setSearch] = useState(searchParams.get('search') || '')
  const { user, can } = useAccess()
  // Filters and sort live in the address, so reload, Back, and shared links keep
  // them. Each change replaces the entry: dragging the size slider is not history.
  const filterKey = CATALOG_FILTER_KEYS.map((key) => searchParams.get(key) || '').join('\n')
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const filters = useMemo<ModelFilterState>(() => readCatalogFilters(searchParams), [filterKey])
  const defaultSort = user.preferences?.catalog_sort || 'updated'
  const urlSort = searchParams.get('sort') || ''
  const sort = SORTS.includes(urlSort) ? urlSort : defaultSort
  const setFilters = useCallback(
    (next: ModelFilterState) => setSearchParams((current) => writeCatalogFilters(next, current), { replace: true }),
    [setSearchParams],
  )
  const setSort = useCallback(
    (value: string) =>
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current)
          if (value === defaultSort) next.delete('sort')
          else next.set('sort', value)
          return next
        },
        { replace: true },
      ),
    [setSearchParams, defaultSort],
  )
  const [models, setModels] = useState<LibraryModel[]>([])
  const [facets, setFacets] = useState<LibraryFacets>({ tasks: {}, precision: {}, hardware: [] })
  const [libraryTotal, setLibraryTotal] = useState(0)
  const [libraryBytes, setLibraryBytes] = useState(0)
  const [loading, setLoading] = useState(true)
  const [scanning, setScanning] = useState(false)
  const [error, setError] = useState('')
  const [mobileFiltersOpen, setMobileFiltersOpen] = useState(false)
  const [filtersHidden, setFiltersHidden] = useState(readFiltersHidden)
  const hideButton = useRef<HTMLButtonElement>(null)
  const showButton = useRef<HTMLButtonElement>(null)
  const mobileFilterButton = useRef<HTMLButtonElement>(null)
  const mobileFilterClose = useRef<HTMLButtonElement>(null)
  const focusAfterToggle = useRef(false)
  const [saving, setSaving] = useState<string | null>(null)
  const latestRequest = useRef(0)
  const navigate = useNavigate()
  const urlSearch = searchParams.get('search') || ''
  // Models made from one model, opened from its Model tree; only ever set by a link.
  const lineageBase = searchParams.get('base_model') || ''
  const lineageRelation = searchParams.get('relation') || ''
  const legacyModel = searchParams.get('model')

  // The address follows the box as it is typed, so reload, Back and a shared link
  // keep the search. A change made elsewhere (Back, a link) replaces the box, but
  // the address trimming a trailing space must not eat the one being typed.
  useEffect(() => {
    setSearch((current) => (current.trim() === urlSearch ? current : urlSearch))
  }, [urlSearch])

  const writeSearch = useCallback(
    (value: string) => setSearchParams((current) => writeCatalogSearch(value, current), { replace: true }),
    [setSearchParams],
  )

  useEffect(() => {
    if (search.trim() === urlSearch) return
    const timer = window.setTimeout(() => writeSearch(search), 250)
    return () => window.clearTimeout(timer)
  }, [search, urlSearch, writeSearch])

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
    if (lineageBase) {
      params.set('base_model', lineageBase)
      if (lineageRelation) params.set('relation', lineageRelation)
    }
    // A slower answer to an earlier search must not replace a newer one.
    const request = ++latestRequest.current
    setLoading(true)
    setError('')
    api
      .libraryModels(params)
      .then((payload) => {
        if (request !== latestRequest.current) return
        setModels(payload.items)
        setFacets(payload.facets)
        setLibraryTotal(payload.total)
        setLibraryBytes(payload.total_bytes)
      })
      .catch((reason) => {
        if (request === latestRequest.current) setError(reason.message)
      })
      .finally(() => {
        if (request === latestRequest.current) setLoading(false)
      })
  }, [filters, search, sort, lineageBase, lineageRelation])

  useEffect(() => {
    const timer = window.setTimeout(fetchModels, 250)
    return () => window.clearTimeout(timer)
  }, [fetchModels])

  function clearSearch() {
    setSearch('')
    writeSearch('')
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

  // Keyboard focus follows the control that took the place of the one pressed;
  // a mouse click leaves focus alone, so no focus ring appears.
  useEffect(() => {
    if (!focusAfterToggle.current) return
    focusAfterToggle.current = false
    ;(filtersHidden ? showButton : hideButton).current?.focus({ preventScroll: true })
  }, [filtersHidden])

  function hideFilters(hidden: boolean, pressed: HTMLElement) {
    focusAfterToggle.current = pressed.matches(':focus-visible')
    setFiltersHidden(hidden)
    try {
      if (hidden) localStorage.setItem(FILTERS_HIDDEN_KEY, '1')
      else localStorage.removeItem(FILTERS_HIDDEN_KEY)
    } catch {
      // Private windows may refuse storage; the choice then lasts until reload.
    }
  }
  // On phones the filters unfold above the results: focus moves into them, and
  // closing (the button or Escape) hands it back to the Filters button.
  function openMobileFilters() {
    setMobileFiltersOpen(true)
    window.requestAnimationFrame(() => mobileFilterClose.current?.focus({ preventScroll: true }))
  }

  function closeMobileFilters() {
    setMobileFiltersOpen(false)
    mobileFilterButton.current?.focus({ preventScroll: true })
  }

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
      onToast(reason instanceof Error ? reason.message : 'Could not update your saved models.', 'error')
    } finally {
      setSaving(null)
    }
  }

  return (
    <>
      <div className={filtersHidden ? 'catalog-layout filters-hidden' : 'catalog-layout'}>
        <aside
          id="catalog-filters"
          className={mobileFiltersOpen ? 'filters mobile-open' : 'filters'}
          aria-label="Filters"
          onKeyDown={(event) => {
            if (event.key === 'Escape' && mobileFiltersOpen) {
              event.stopPropagation()
              closeMobileFilters()
            }
          }}
        >
          <div className="filters-inner">
            <div className="filters-heading">
              <Filter size={16} />
              <span>Models</span>
              {activeFilters > 0 && <em>{activeFilters}</em>}
              <button
                ref={hideButton}
                type="button"
                className="filter-hide"
                tabIndex={filtersHidden ? -1 : undefined}
                onClick={(event) => hideFilters(true, event.currentTarget)}
                aria-label="Hide filters"
                title="Hide filters"
              >
                <PanelLeftClose size={16} />
              </button>
              <button
                ref={mobileFilterClose}
                type="button"
                className="filter-mobile-close"
                onClick={closeMobileFilters}
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
          </div>
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
            <button
              ref={showButton}
              type="button"
              className="secondary-button show-filters-button"
              onClick={(event) => hideFilters(false, event.currentTarget)}
              tabIndex={filtersHidden ? undefined : -1}
              aria-hidden={filtersHidden ? undefined : true}
            >
              <PanelLeftOpen size={15} />
              Filters {activeFilters > 0 ? `(${activeFilters})` : ''}
            </button>
            <div className="catalog-search">
              <Search size={18} />
              <input
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') writeSearch(search)
                }}
                aria-label="Search models"
                placeholder="Search model names, owners, tasks, and tags"
              />
              {search && (
                <button type="button" onClick={clearSearch} aria-label="Clear search">
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
              ref={mobileFilterButton}
              type="button"
              className="secondary-button mobile-filter-button"
              aria-expanded={mobileFiltersOpen}
              aria-controls="catalog-filters"
              onClick={() => (mobileFiltersOpen ? closeMobileFilters() : openMobileFilters())}
            >
              <ListFilter size={15} />
              Filters {activeFilters > 0 ? `(${activeFilters})` : ''}
            </button>
          </div>

          {lineageBase && (
            <div className="lineage-filter" role="status">
              <GitFork size={14} />
              <span>
                {lineageRelation ? relationGroup(lineageRelation, 2) : 'Models made from'}
                {lineageRelation ? ' of ' : ' '}
                <Link to={`/models/${lineageBase}`}>{lineageBase}</Link>
              </span>
              <button
                type="button"
                aria-label="Show every model"
                title="Show every model"
                onClick={() => {
                  const next = new URLSearchParams(searchParams)
                  next.delete('base_model')
                  next.delete('relation')
                  setSearchParams(next)
                }}
              >
                <CircleX size={15} />
              </button>
            </div>
          )}
          <div className="results-line">
            <span>
              {scanning
                ? 'Scanning storage for new or changed models…'
                : loading
                ? 'Reading the local library…'
                : models.length === libraryTotal
                  ? `${models.length} model${models.length === 1 ? '' : 's'}`
                  : `${models.length} of ${libraryTotal} model${libraryTotal === 1 ? '' : 's'} shown`}
            </span>
            <span>{formatBytes(libraryBytes)} stored locally</span>
          </div>

          {error && (
            <LoadError what="the models" message={error} onRetry={fetchModels} />
          )}
          {/* A scan re-reads every model, so the grid shows what is coming instead. */}
          {loading || scanning ? (
            <ModelCardSkeletons label={scanning ? 'Scanning the library' : 'Loading models'} />
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
  theme,
  onAuthChange,
  onLogout,
}: {
  authStatus: AuthStatus
  theme: Theme
  onAuthChange: (status: AuthStatus) => void
  onLogout: () => void
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

  return (
    <AccessProvider user={user} capabilities={capabilities} refresh={refreshAccess}>
    <ConfirmProvider>
    <UploadProvider onToast={showToast}>
    <Shell user={user} theme={theme} onLogout={onLogout}>
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
        <Route path="/admin/users/:userId" element={<AdminPage onToast={showToast} />} />
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
    </ConfirmProvider>
    </AccessProvider>
  )
}

export default function App() {
  const [status, setStatus] = useState<AuthStatus | null>(null)
  const [error, setError] = useState('')
  // Why the sign-in page is showing, when it is not the obvious reason.
  const [notice, setNotice] = useState('')
  // Signed in, an account without a saved theme follows the device, as its
  // Preferences say; signed out, this browser's last choice applies.
  const theme = useAppTheme(status?.user ? status.user.preferences?.theme || 'system' : undefined)
  const signedIn = useRef(false)
  signedIn.current = Boolean(status?.user)
  const signingOut = useRef(false)

  const refreshAuth = useCallback(() => {
    setError('')
    api
      .authStatus()
      .then((next) => {
        setStatus(next)
        if (next.user) setNotice('')
      })
      .catch((reason) => setError(reason instanceof Error ? reason.message : 'Could not reach HuggingHack.'))
  }, [])

  // A session that ends mid-visit keeps the app mounted, hidden behind the
  // sign-in form, so a running upload keeps its files: signing back in to the
  // same account lets it resume. Another account starts afresh (the app is keyed
  // by user id).
  const [kept, setKept] = useState<AuthStatus | null>(null)
  const current = useRef<AuthStatus | null>(null)
  if (status?.user) current.current = status
  const keeping = useRef(false)
  keeping.current = Boolean(kept && !status?.user)
  // Signed in again: what was kept is on screen now, or was another account's.
  useEffect(() => {
    if (status?.user) setKept(null)
  }, [status])

  useEffect(() => {
    refreshAuth()
    // A request refused mid-visit means the session ended under us.
    const expired = () => {
      if (signedIn.current && !signingOut.current) {
        setNotice('Your session expired. Sign in again.')
        setKept(current.current)
      } else if (keeping.current) {
        // The hidden app's own requests are refused too; the sign-in form is already up.
        return
      }
      refreshAuth()
    }
    window.addEventListener('hugginghack:unauthorized', expired)
    return () => window.removeEventListener('hugginghack:unauthorized', expired)
  }, [refreshAuth])

  async function logout() {
    if (!status) return
    // Signing out of a session that already ended is still a sign-out, not an expiry.
    signingOut.current = true
    try {
      await api.logout()
    } finally {
      // The next person to sign in starts from the front page, not this account's last one.
      crossfade(() => {
        window.history.replaceState(null, '', '#/')
        setStatus({ ...status, user: null, csrf_token: null })
      })
      signingOut.current = false
    }
  }

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
        <img src={brandMark(theme)} alt="" />
        <LoaderCircle size={23} className="spin" />
        <span>Opening your model library…</span>
      </main>
    )
  }

  const shown = status.user && !status.setup_required ? status : null
  const app = shown || kept
  // The app keeps its place in the tree while the sign-in form is up, so it is not remounted.
  return (
    <>
      {app?.user && (
        <div key={app.user.id} className="app-frame" hidden={!shown}>
          <HashRouter>
            <Application authStatus={app} theme={theme} onAuthChange={setStatus} onLogout={logout} />
          </HashRouter>
        </div>
      )}
      {!shown && (
        <AuthScreen
          theme={theme}
          setup={status.setup_required}
          oidc={status.oidc}
          notice={notice}
          onAuthenticated={(next) => {
            setNotice('')
            setStatus(next)
          }}
        />
      )}
    </>
  )
}
