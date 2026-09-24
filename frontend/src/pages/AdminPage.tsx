import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import {
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleX,
  KeyRound,
  LoaderCircle,
  LogOut,
  Minus,
  Plus,
  Search,
  ShieldAlert,
  SlidersHorizontal,
  Trash2,
  UserCheck,
  UserPlus,
  UserX,
  Wifi,
  X,
} from 'lucide-react'
import { Link, NavLink, Navigate, useParams, useSearchParams } from 'react-router-dom'
import { useAccess } from '../access'
import { api } from '../api'
import type { AdminUser, AdminUserPage, AdminUserQuery, AdminUserSort, Organization, PermissionMatrix, Role, ServerSettings } from '../types'
import { pageList } from '../pagination'
import { relativeTime } from '../utils'
import { RuntimesPage } from './RuntimesPage'
import { StoragePage } from './StoragePage'

type ToastHandler = (message: string, tone?: 'success' | 'error') => void

const ROLE_LABELS: Record<Role, string> = { admin: 'Administrator', member: 'Member', viewer: 'Viewer' }

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback
}

const PAGE_SIZES = [10, 25, 50, 100]
const PAGE_SIZE_KEY = 'hugginghack.admin.users.per-page'
const SORT_LABELS: Record<AdminUserSort, string> = {
  role: 'Role',
  name: 'Name',
  last_login: 'Last sign-in',
  newest: 'Newest',
}
const ROLE_FILTERS: Array<{ id: '' | Role; label: string }> = [
  { id: '', label: 'All' },
  { id: 'admin', label: 'Administrators' },
  { id: 'member', label: 'Members' },
  { id: 'viewer', label: 'Viewers' },
]

function storedPageSize(): number {
  try {
    const value = Number(window.localStorage.getItem(PAGE_SIZE_KEY))
    return PAGE_SIZES.includes(value) ? value : 25
  } catch {
    return 25
  }
}

function readUserQuery(params: URLSearchParams): AdminUserQuery {
  const role = params.get('role') || ''
  const status = params.get('status') || ''
  const sort = params.get('sort') || ''
  const perPage = Number(params.get('per_page'))
  return {
    q: params.get('q') || '',
    role: role in ROLE_LABELS ? (role as Role) : '',
    status: status === 'active' || status === 'disabled' ? status : '',
    sort: sort in SORT_LABELS ? (sort as AdminUserSort) : 'role',
    page: Math.max(1, Math.floor(Number(params.get('page'))) || 1),
    per_page: PAGE_SIZES.includes(perPage) ? perPage : storedPageSize(),
  }
}

const EMPTY_ACCOUNT = { username: '', display_name: '', email: '', password: '', role: 'member' as Role }

function AddUserDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (username: string) => void }) {
  const [form, setForm] = useState(EMPTY_ACCOUNT)
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState('')
  const firstField = useRef<HTMLInputElement>(null)

  useEffect(() => {
    firstField.current?.focus()
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', escape)
    return () => window.removeEventListener('keydown', escape)
  }, [onClose])

  async function create(event: FormEvent) {
    event.preventDefault()
    setCreating(true)
    setError('')
    try {
      await api.createUser({ ...form, email: form.email.trim() || undefined })
      onCreated(form.username)
    } catch (reason) {
      setError(errorMessage(reason, 'Could not create the account.'))
      setCreating(false)
    }
  }

  return (
    <div className="use-model-backdrop" role="presentation" onMouseDown={onClose}>
      <form
        className="use-model-dialog add-user-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="add-user-title"
        onMouseDown={(event) => event.stopPropagation()}
        onSubmit={create}
      >
        <header className="use-model-header">
          <div>
            <span className="eyebrow">New account</span>
            <h2 id="add-user-title">Add a user</h2>
          </div>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Close">
            <X size={20} />
          </button>
        </header>
        <div className="use-model-body">
          <div className="admin-create-user">
            <label>
              <span>Username</span>
              <input ref={firstField} value={form.username} onChange={(event) => setForm({ ...form, username: event.target.value })} autoComplete="off" required />
            </label>
            <label>
              <span>Display name <small>optional</small></span>
              <input value={form.display_name} onChange={(event) => setForm({ ...form, display_name: event.target.value })} />
            </label>
            <label>
              <span>Email <small>optional</small></span>
              <input type="email" value={form.email} onChange={(event) => setForm({ ...form, email: event.target.value })} />
            </label>
            <label>
              <span>Role</span>
              <select value={form.role} onChange={(event) => setForm({ ...form, role: event.target.value as Role })}>
                {Object.entries(ROLE_LABELS).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
              </select>
            </label>
            <label className="wide">
              <span>Temporary password <small>at least 12 characters</small></span>
              <input type="password" autoComplete="new-password" minLength={12} value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} required />
            </label>
          </div>
          {error && <div className="inline-error add-user-error">{error}</div>}
          <div className="add-user-footer">
            <span>They can change the password after signing in.</span>
            <button type="button" className="secondary-button" onClick={onClose}>Cancel</button>
            <button type="submit" className="download-button" disabled={creating}>
              {creating ? <LoaderCircle size={16} className="spin" /> : <UserPlus size={16} />} Create account
            </button>
          </div>
        </div>
      </form>
    </div>
  )
}

function UsersTab({ onToast }: { onToast: ToastHandler }) {
  const { user: me } = useAccess()
  const [params, setParams] = useSearchParams()
  const query = useMemo(() => readUserQuery(params), [params])
  const [search, setSearch] = useState(query.q)
  const [result, setResult] = useState<AdminUserPage | null>(null)
  const [loading, setLoading] = useState(true)
  const [accountsEnabled, setAccountsEnabled] = useState(true)
  const [busy, setBusy] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const latest = useRef(0)
  const users = result?.items || []

  const update = useCallback(
    (changes: Partial<AdminUserQuery>, replace = false) => {
      const next = { ...query, page: 1, ...changes }
      const values = new URLSearchParams()
      if (next.q.trim()) values.set('q', next.q.trim())
      if (next.role) values.set('role', next.role)
      if (next.status) values.set('status', next.status)
      if (next.sort !== 'role') values.set('sort', next.sort)
      if (next.page > 1) values.set('page', String(next.page))
      if (next.per_page !== 25) values.set('per_page', String(next.per_page))
      setParams(values, { replace })
    },
    [query, setParams],
  )

  const load = useCallback(() => {
    const request = ++latest.current
    setLoading(true)
    api
      .adminUsers(query)
      .then((payload) => {
        if (request !== latest.current) return
        setResult(payload)
        setAccountsEnabled(payload.accounts_enabled)
        // The server moves past-the-end pages back to the last one (after a delete, say).
        if (payload.page !== query.page) update({ page: payload.page }, true)
      })
      .catch((reason) => {
        if (request === latest.current) onToast(errorMessage(reason, 'Could not load accounts.'), 'error')
      })
      .finally(() => {
        if (request === latest.current) setLoading(false)
      })
  }, [query, onToast, update])

  useEffect(() => {
    load()
  }, [load])

  // The address is the source of truth: Back, Forward, or the Users tab link
  // replace the search box text instead of the box re-applying an old search.
  useEffect(() => {
    setSearch((current) => (current.trim() === query.q ? current : query.q))
  }, [query.q])

  useEffect(() => {
    if (search.trim() === query.q) return
    const timer = window.setTimeout(() => update({ q: search }, true), 250)
    return () => window.clearTimeout(timer)
  }, [search, query.q, update])

  function changePageSize(value: number) {
    try {
      window.localStorage.setItem(PAGE_SIZE_KEY, String(value))
    } catch {
      // Remembering the page size is a convenience only.
    }
    // Keep the first account on screen in view after the page size changes.
    const firstIndex = (query.page - 1) * query.per_page
    update({ per_page: value, page: Math.floor(firstIndex / value) + 1 })
  }

  function clearFilters() {
    setSearch('')
    update({ q: '', role: '', status: '' })
  }

  async function act(user: AdminUser, action: () => Promise<unknown>, message: string) {
    setBusy(user.id)
    try {
      await action()
      onToast(message)
      load()
    } catch (reason) {
      onToast(errorMessage(reason, 'That change was not saved.'), 'error')
    } finally {
      setBusy(null)
    }
  }

  function resetPassword(user: AdminUser) {
    const password = window.prompt(
      `New password for ${user.username} (at least 12 characters). Their sessions will be signed out.`,
    )
    if (!password) return
    act(user, () => api.adminResetPassword(user.id, password), `${user.username}'s password was reset.`)
  }

  function remove(user: AdminUser) {
    const external = (user.auth_provider || 'local') !== 'local'
    const warning = external
      ? '\n\nThis account signs in through single sign-on and will be created again the next time they sign in. Disable it instead to block access.'
      : ''
    if (window.prompt(`Type ${user.username} to delete this account permanently.${warning}`) !== user.username) return
    act(user, () => api.adminDeleteUser(user.id), `${user.username} was deleted.`)
  }

  return (
    <>
      <section className="settings-section admin-users">
        <div className="section-heading-line">
          <div>
            <span className="eyebrow">
              {result ? `${result.counts.all} account${result.counts.all === 1 ? '' : 's'}${query.q ? ' match' : ''}` : 'Accounts'}
            </span>
            <h2>Users</h2>
          </div>
          {accountsEnabled && (
            <button type="button" className="download-button compact" onClick={() => setAdding(true)}>
              <UserPlus size={15} /> Add user
            </button>
          )}
        </div>
        <div className="admin-user-toolbar">
          <div className="catalog-search compact">
            <Search size={16} />
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search name, username, or email"
              aria-label="Search accounts"
            />
            {search && (
              <button type="button" onClick={() => setSearch('')} aria-label="Clear search">
                <CircleX size={15} />
              </button>
            )}
          </div>
          <label className="sort-control compact">
            <select
              value={query.status}
              onChange={(event) => update({ status: event.target.value as AdminUserQuery['status'] })}
              aria-label="Filter by status"
            >
              <option value="">Any status</option>
              <option value="active">Active{result ? ` (${result.counts.active})` : ''}</option>
              <option value="disabled">Disabled{result ? ` (${result.counts.disabled})` : ''}</option>
            </select>
            <ChevronDown size={14} />
          </label>
          <label className="sort-control compact">
            <SlidersHorizontal size={14} />
            <select
              value={query.sort}
              onChange={(event) => update({ sort: event.target.value as AdminUserSort })}
              aria-label="Sort accounts"
            >
              {Object.entries(SORT_LABELS).map(([id, label]) => <option key={id} value={id}>Sort: {label}</option>)}
            </select>
            <ChevronDown size={14} />
          </label>
        </div>
        <div className="role-filter" role="group" aria-label="Filter by role">
          {ROLE_FILTERS.map((filter) => (
            <button
              key={filter.id || 'all'}
              type="button"
              className={query.role === filter.id ? 'selected' : undefined}
              aria-pressed={query.role === filter.id}
              onClick={() => update({ role: filter.id })}
            >
              {filter.label}
              {result && <span>{filter.id ? result.counts[filter.id] : result.counts.all}</span>}
            </button>
          ))}
        </div>
        <div className={loading && result ? 'admin-user-table refreshing' : 'admin-user-table'} role="table" aria-label="Accounts" aria-busy={loading}>
          <div className="admin-user-row header" role="row">
            <span>Account</span><span>Role</span><span>Status</span><span>Last sign-in</span><span>Access</span><span />
          </div>
          {!result && loading && (
            <div className="admin-user-empty"><LoaderCircle size={20} className="spin" /></div>
          )}
          {result && users.length === 0 && (
            <div className="admin-user-empty">
              <strong>No accounts match these filters.</strong>
              <button type="button" className="secondary-button compact" onClick={clearFilters}>Clear filters</button>
            </div>
          )}
          {users.map((user) => {
            const self = user.id === me.id
            return (
              <div className={user.disabled ? 'admin-user-row disabled' : 'admin-user-row'} role="row" key={user.id}>
                <span className="admin-user-name">
                  <strong>{user.display_name}{self ? ' (you)' : ''}</strong>
                  <small>@{user.username}{user.email ? ` · ${user.email}` : ''}{user.auth_provider && user.auth_provider !== 'local' ? ` · ${user.auth_provider}` : ''}</small>
                </span>
                <span>
                  <select
                    value={user.role}
                    disabled={self || busy === user.id}
                    aria-label={`Role for ${user.username}`}
                    onChange={(event) =>
                      act(user, () => api.adminUpdateUser(user.id, { role: event.target.value }), `${user.username} is now ${ROLE_LABELS[event.target.value as Role].toLowerCase()}.`)
                    }
                  >
                    {Object.entries(ROLE_LABELS).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
                  </select>
                </span>
                <span>
                  <span className={user.disabled ? 'status-pill danger' : 'status-pill ok'}>
                    {user.disabled ? 'Disabled' : 'Active'}
                  </span>
                </span>
                <span className="admin-user-muted">{user.last_login_at ? relativeTime(user.last_login_at) : 'Never'}</span>
                <span className="admin-user-muted">
                  {user.sessions} session{user.sessions === 1 ? '' : 's'} · {user.tokens} token{user.tokens === 1 ? '' : 's'} · {user.repositories} repo{user.repositories === 1 ? '' : 's'}
                </span>
                <span className="admin-user-actions">
                  {busy === user.id && <LoaderCircle size={15} className="spin" />}
                  {!self && (
                    <button
                      type="button"
                      title={user.disabled ? 'Enable account' : 'Disable account'}
                      aria-label={user.disabled ? `Enable ${user.username}` : `Disable ${user.username}`}
                      onClick={() => act(user, () => api.adminUpdateUser(user.id, { disabled: !user.disabled }), `${user.username} was ${user.disabled ? 'enabled' : 'disabled'}.`)}
                    >
                      {user.disabled ? <UserCheck size={15} /> : <UserX size={15} />}
                    </button>
                  )}
                  {accountsEnabled && (user.auth_provider || 'local') === 'local' && (
                    <button type="button" title="Reset password" aria-label={`Reset password for ${user.username}`} onClick={() => resetPassword(user)}>
                      <KeyRound size={15} />
                    </button>
                  )}
                  <button
                    type="button"
                    title="Sign out everywhere and revoke API tokens"
                    aria-label={`Revoke sessions and tokens for ${user.username}`}
                    onClick={() => {
                      if (window.confirm(`Sign ${user.username} out everywhere and revoke their API tokens?`)) {
                        act(user, () => api.adminRevoke(user.id, { sessions: true, tokens: true }), `${user.username} was signed out everywhere.`)
                      }
                    }}
                  >
                    <LogOut size={15} />
                  </button>
                  {!self && (
                    <button type="button" className="danger-text" title="Delete account" aria-label={`Delete ${user.username}`} onClick={() => remove(user)}>
                      <Trash2 size={15} />
                    </button>
                  )}
                </span>
              </div>
            )
          })}
        </div>
        {result && result.total > 0 && (
          <div className="admin-user-pager">
            <span className="admin-user-muted">
              Showing {(result.page - 1) * result.per_page + 1}–{Math.min(result.page * result.per_page, result.total)} of {result.total}
            </span>
            <label className="pager-size">
              Rows per page
              <span className="sort-control compact">
                <select value={query.per_page} onChange={(event) => changePageSize(Number(event.target.value))} aria-label="Rows per page">
                  {PAGE_SIZES.map((size) => <option key={size} value={size}>{size}</option>)}
                </select>
                <ChevronDown size={14} />
              </span>
            </label>
            {result.pages > 1 && (
              <nav className="pager" aria-label="Account pages">
                <button type="button" disabled={result.page <= 1} onClick={() => update({ page: result.page - 1 })} aria-label="Previous page">
                  <ChevronLeft size={15} />
                </button>
                {pageList(result.page, result.pages).map((page, index) =>
                  page === null ? (
                    <span key={`gap-${index}`} className="pager-gap">…</span>
                  ) : (
                    <button
                      key={page}
                      type="button"
                      className={page === result.page ? 'current' : undefined}
                      aria-current={page === result.page ? 'page' : undefined}
                      onClick={() => update({ page })}
                    >
                      {page}
                    </button>
                  ),
                )}
                <button type="button" disabled={result.page >= result.pages} onClick={() => update({ page: result.page + 1 })} aria-label="Next page">
                  <ChevronRight size={15} />
                </button>
              </nav>
            )}
          </div>
        )}
      </section>
      {adding && (
        <AddUserDialog
          onClose={() => setAdding(false)}
          onCreated={(username) => {
            setAdding(false)
            onToast(`${username} can now sign in.`)
            load()
          }}
        />
      )}
    </>
  )
}

function RolesTab() {
  const [matrix, setMatrix] = useState<PermissionMatrix | null>(null)
  useEffect(() => {
    api.permissions().then(setMatrix).catch(() => undefined)
  }, [])
  if (!matrix) return <div className="drawer-loading"><LoaderCircle size={22} className="spin" /></div>
  return (
    <section className="settings-section">
      <div className="section-heading-line">
        <div>
          <span className="eyebrow">Enforced by the server for the web, API tokens, and pulls</span>
          <h2>Roles &amp; permissions</h2>
        </div>
      </div>
      <div className="role-cards">
        {matrix.roles.map((role) => (
          <div key={role.id} className="role-card">
            <span className={`role-badge ${role.id}`}>{ROLE_LABELS[role.id]}</span>
            <p>{role.description}</p>
          </div>
        ))}
      </div>
      <div className="permission-table" role="table" aria-label="Permission matrix">
        <div className="permission-row header" role="row">
          <span>Permission</span>
          {matrix.roles.map((role) => <span key={role.id}>{ROLE_LABELS[role.id]}</span>)}
        </div>
        {matrix.capabilities.map((capability) => (
          <div className="permission-row" role="row" key={capability.id}>
            <span>
              {capability.description}
              <code>{capability.id}</code>
            </span>
            {matrix.roles.map((role) => (
              <span key={role.id} className="permission-cell">
                {role.capabilities.includes(capability.id)
                  ? <Check size={16} aria-label="Allowed" className="good-text" />
                  : <Minus size={16} aria-label="Not allowed" className="permission-denied" />}
              </span>
            ))}
          </div>
        ))}
      </div>
      <p className="account-note">
        Anonymous pulls (vLLM, git, and the hf CLI without a token) can read models that every
        account can see; private uploads always need their owner&apos;s token.
      </p>
    </section>
  )
}

function Fact({ label, value, good }: { label: string; value: string; good?: boolean }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd className={good ? 'good-text' : undefined}>{value}</dd>
    </div>
  )
}

function ServerTab() {
  const [server, setServer] = useState<ServerSettings | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    api.serverSettings().then(setServer).catch((reason) => setError(reason.message))
  }, [])
  if (error) return <div className="inline-error">{error}</div>
  if (!server) return <div className="drawer-loading"><LoaderCircle size={22} className="spin" /></div>
  const yes = (value: boolean) => (value ? 'Yes' : 'No')
  return (
    <>
      <p className="account-note server-note">
        These settings come from <code>.env</code>. Change them there and run <code>docker compose up -d</code>.
        Secret values are never shown, only whether they are set.
      </p>
      <div className="settings-columns">
        <section className="settings-section">
          <h2>Accounts &amp; sign-in</h2>
          <dl className="settings-list">
            <Fact label="Accounts" value={server.accounts.enabled ? 'Enabled' : 'Disabled (single user)'} good={server.accounts.enabled} />
            <Fact label="Secure cookies (HTTPS)" value={yes(server.accounts.secure_cookies)} good={server.accounts.secure_cookies} />
            <Fact label="Session length" value={`${server.accounts.session_ttl_hours} hours`} />
          </dl>
        </section>
        <section className="settings-section">
          <h2>Single sign-on (OpenID Connect)</h2>
          <dl className="settings-list">
            <Fact label="Status" value={server.sso.enabled ? `Enabled · ${server.sso.provider_name}` : 'Not configured'} good={server.sso.enabled} />
            {server.sso.enabled && (
              <>
                <Fact label="Issuer" value={server.sso.issuer || ''} />
                <Fact label="Client ID" value={server.sso.client_id || ''} />
                <Fact label="Client secret" value={server.sso.client_secret_configured ? 'Configured' : 'Not set (public client)'} />
                <Fact label="Redirect URL" value={server.sso.redirect_url || 'Browser address + /api/auth/oidc/callback'} />
                <Fact label="New accounts get" value={ROLE_LABELS[server.sso.default_role as Role] || server.sso.default_role} />
                <Fact label="Allowed groups" value={server.sso.allowed_groups.join(', ') || 'Everyone the provider lets in'} />
              </>
            )}
          </dl>
        </section>
        <section className="settings-section">
          <h2>Database</h2>
          <dl className="settings-list">
            <Fact label="Engine" value={server.database.backend === 'postgresql' ? 'PostgreSQL' : 'SQLite'} />
            <Fact label="Location" value={server.database.target} />
          </dl>
        </section>
        <section className="settings-section">
          <h2>Pulling models</h2>
          <dl className="settings-list">
            <Fact label="Anonymous pulls" value={server.pulls.hub_api_enabled ? 'Allowed' : 'Tokens only'} />
            <Fact label="Public address" value={server.pulls.public_url || 'Browser address'} />
            <Fact label="Upload chunk" value={`${server.uploads.chunk_mb} MB`} />
            <Fact label="Largest file" value={`${server.uploads.max_file_gb} GB`} />
          </dl>
        </section>
        <section className="settings-section">
          <h2>Storage</h2>
          <dl className="settings-list">
            <Fact label="Model folder" value={server.storage.model_path} />
            <Fact label="App data" value={server.storage.data_path} />
            <Fact label="Default location" value={server.storage.default_target} />
            {server.storage.targets.map((target) => (
              <Fact
                key={target.id}
                label={target.name}
                value={target.kind === 's3'
                  ? `s3://${target.bucket}${target.prefix ? `/${target.prefix}` : ''} · credentials ${target.credentials_configured ? 'set' : 'from environment'}`
                  : target.path || ''}
              />
            ))}
          </dl>
        </section>
        <section className="settings-section">
          <h2>Hugging Face</h2>
          <dl className="settings-list">
            <Fact label="Endpoint" value={server.hugging_face.endpoint} />
            <Fact label="Access token" value={server.hugging_face.token_configured ? 'Configured' : 'Not set'} good={server.hugging_face.token_configured} />
            <Fact label="Parallel downloads" value={`${server.hugging_face.max_concurrent_downloads} × ${server.hugging_face.workers_per_download} workers`} />
          </dl>
        </section>
        <section className="settings-section">
          <h2>Runtimes</h2>
          <dl className="settings-list">
            <Fact label="Destinations" value={String(server.runtimes.targets.length)} />
            {server.runtimes.targets.map((target) => (
              <Fact key={target.id} label={target.name} value={`${target.kind} · ${target.base_url}`} />
            ))}
            <Fact label="Automation token" value={server.runtimes.api_token_configured ? 'Configured' : 'Not set'} />
          </dl>
        </section>
      </div>
      <section className="settings-section security-section">
        <div className="settings-section-title">
          <ShieldAlert size={20} />
          <div>
            <h2>Network safety</h2>
            <p>Model files are data until another program loads them.</p>
          </div>
        </div>
        <div className="security-columns">
          <div>
            <strong>HuggingHack never</strong>
            <p>imports model code, unpickles weights, executes repositories, or sends your files elsewhere.</p>
          </div>
          <div>
            <strong>Before exposing it widely</strong>
            <p>keep accounts enabled, serve it through an HTTPS reverse proxy, and turn on secure cookies.</p>
          </div>
          <div>
            <strong>Pulls without a token</strong>
            <p>can read every model all accounts can see. Set HUB_API_ENABLED=false to require tokens.</p>
          </div>
        </div>
      </section>
      <div className="runtime-line">
        <Wifi size={15} />
        {server.app} {server.version} · unofficial, local-first, and not affiliated with Hugging Face
      </div>
    </>
  )
}

function OrganizationsTab({ onToast }: { onToast: ToastHandler }) {
  const [items, setItems] = useState<Organization[]>([])
  const [form, setForm] = useState({ name: '', display_name: '', description: '' })
  const [saving, setSaving] = useState(false)

  const load = useCallback(() => {
    api.organizations().then((payload) => setItems(payload.items)).catch(() => undefined)
  }, [])

  useEffect(() => {
    load()
  }, [load])

  async function create(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    try {
      await api.createOrganization(form)
      onToast(`${form.name} was created. You are its first admin.`)
      setForm({ name: '', display_name: '', description: '' })
      load()
    } catch (reason) {
      onToast(errorMessage(reason, 'Could not create the organization.'), 'error')
    } finally {
      setSaving(false)
    }
  }

  async function remove(organization: Organization) {
    if (window.prompt(`Type ${organization.name} to delete this organization. It must have no repositories.`) !== organization.name) return
    try {
      await api.deleteOrganization(organization.name)
      onToast(`${organization.name} was deleted.`)
      load()
    } catch (reason) {
      onToast(errorMessage(reason, 'Could not delete the organization.'), 'error')
    }
  }

  return (
    <>
      <section className="settings-section">
        <div className="section-heading-line">
          <div>
            <span className="eyebrow">{items.length} organization{items.length === 1 ? '' : 's'}</span>
            <h2>Organizations</h2>
          </div>
        </div>
        <div className="admin-user-table" role="table" aria-label="Organizations">
          <div className="admin-user-row org-row header" role="row">
            <span>Organization</span><span>Repositories</span><span>Members</span><span>Your role</span><span />
          </div>
          {items.map((organization) => (
            <div className="admin-user-row org-row" role="row" key={organization.id}>
              <span className="admin-user-name">
                <Link to={`/orgs/${organization.name}`}><strong>{organization.display_name}</strong></Link>
                <small>@{organization.name}{organization.description ? ` · ${organization.description}` : ''}</small>
              </span>
              <span className="admin-user-muted">{organization.repository_count || 0}</span>
              <span className="admin-user-muted">{organization.member_count || 0}</span>
              <span className="admin-user-muted">{organization.my_role || '—'}</span>
              <span className="admin-user-actions">
                <Link to={`/orgs/${organization.name}/members`} className="secondary-button compact">Members</Link>
                <button type="button" className="danger-text" aria-label={`Delete ${organization.name}`} title="Delete organization" onClick={() => remove(organization)}>
                  <Trash2 size={15} />
                </button>
              </span>
            </div>
          ))}
          {items.length === 0 && <div className="empty-compact">No organizations yet.</div>}
        </div>
      </section>
      <section className="settings-section">
        <div className="settings-section-title">
          <Plus size={20} />
          <div>
            <h2>New organization</h2>
            <p>Its name becomes a namespace like <code>Nvidia/GLM-5.3-NVFP4</code>. It cannot be renamed later.</p>
          </div>
        </div>
        <form className="admin-create-user" onSubmit={create}>
          <label>Name<input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="Nvidia" required /></label>
          <label>Display name<input value={form.display_name} onChange={(event) => setForm({ ...form, display_name: event.target.value })} placeholder="NVIDIA" /></label>
          <label>Description <small>optional</small><input value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} /></label>
          <button className="download-button" disabled={saving}>
            {saving ? <LoaderCircle size={16} className="spin" /> : <Plus size={16} />} Create organization
          </button>
        </form>
      </section>
    </>
  )
}

const TABS = [
  { id: 'users', label: 'Users', capability: 'users.manage' },
  { id: 'organizations', label: 'Organizations', capability: 'orgs.manage' },
  { id: 'roles', label: 'Roles & permissions', capability: 'users.manage' },
  { id: 'storage', label: 'Storage', capability: 'storage.view' },
  { id: 'runtimes', label: 'Runtimes', capability: 'runtimes.use' },
  { id: 'server', label: 'Server', capability: 'settings.view' },
]

export function AdminPage({ onToast }: { onToast: ToastHandler }) {
  const { tab } = useParams()
  const { can } = useAccess()
  const tabs = TABS.filter((item) => can(item.capability))
  if (!tabs.length) return <Navigate to="/account" replace />
  const active = tabs.find((item) => item.id === tab)
  if (!active) return <Navigate to={`/admin/${tabs[0].id}`} replace />

  return (
    <div className="section-page">
      <header className="section-hero">
        <div className="section-hero-inner">
          <span className="eyebrow">Administration</span>
          <h1>Server administration</h1>
          <nav className="model-tabs" aria-label="Administration sections">
            <div>
              {tabs.map((item) => (
                <NavLink key={item.id} to={`/admin/${item.id}`} className={({ isActive }) => (isActive ? 'active' : '')}>
                  {item.label}
                </NavLink>
              ))}
            </div>
          </nav>
        </div>
      </header>
      <div className="section-body admin-content">
        {active.id === 'users' && <UsersTab onToast={onToast} />}
        {active.id === 'roles' && <RolesTab />}
        {active.id === 'organizations' && <OrganizationsTab onToast={onToast} />}
        {active.id === 'storage' && <StoragePage onToast={onToast} />}
        {active.id === 'runtimes' && <RuntimesPage />}
        {active.id === 'server' && <ServerTab />}
      </div>
    </div>
  )
}
