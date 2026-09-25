import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import {
  Building2,
  Check,
  ChevronDown,
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
import { Link, NavLink, Navigate, useLocation, useParams } from 'react-router-dom'
import { useAccess } from '../access'
import { api } from '../api'
import type { AdminOrganizationFilter, AdminOrganizationPage, AdminOrganizationQuery, AdminOrganizationSort, AdminUser, AdminUserPage, AdminUserQuery, AdminUserSort, Organization, OrganizationRole, PermissionMatrix, Role, ServerSettings } from '../types'
import { AdminUserDetail } from '../components/AdminUserDetail'
import { DialogFrame } from '../components/Dialog'
import { ListPager, useListQuery } from '../components/ListPager'
import { useClosingTransition, useFadeOnChange, useTabIndicator } from '../motion'
import { relativeTime } from '../utils'
import { ORG_ROLE_LABELS } from './OrganizationPage'
import { RuntimesPage } from './RuntimesPage'
import { StoragePage } from './StoragePage'
import { RowSkeletons } from '../components/Skeletons'
import { useConfirm } from '../components/ConfirmDialog'
import { MarkdownEditor } from '../components/Markdown'
import { markdownSummary } from '../markdownText'

type ToastHandler = (message: string, tone?: 'success' | 'error') => void

const ROLE_LABELS: Record<Role, string> = { admin: 'Administrator', member: 'Member', viewer: 'Viewer' }

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback
}

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

const USER_QUERY_DEFAULTS = { q: '', role: '', status: '', sort: 'role' }
const USER_QUERY_CHOICES = {
  role: ['admin', 'member', 'viewer'],
  status: ['active', 'disabled'],
  sort: Object.keys(SORT_LABELS),
}

const EMPTY_ACCOUNT = { username: '', display_name: '', email: '', password: '', role: 'member' as Role }

type Membership = { organization: string; role: OrganizationRole }

function AddUserDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (username: string, organizations: number) => void }) {
  const { can } = useAccess()
  const [form, setForm] = useState(EMPTY_ACCOUNT)
  const [memberships, setMemberships] = useState<Membership[]>([])
  const [organizations, setOrganizations] = useState<Organization[]>([])
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState('')
  const firstField = useRef<HTMLInputElement>(null)
  const { closing, close } = useClosingTransition(onClose)
  // Only organizations this admin may add members to.
  const choices = organizations.filter((item) => can('orgs.manage') || item.my_role === 'admin')
  const unused = choices.filter((item) => !memberships.some((chosen) => chosen.organization === item.name))
  const limitedByRole = form.role === 'viewer' && memberships.some((item) => item.role !== 'read')

  useEffect(() => {
    api.organizations().then((payload) => setOrganizations(payload.items)).catch(() => undefined)
  }, [])

  function changeMembership(index: number, changes: Partial<Membership>) {
    setMemberships((current) => current.map((item, position) => (position === index ? { ...item, ...changes } : item)))
  }

  useEffect(() => firstField.current?.focus(), [])

  async function create(event: FormEvent) {
    event.preventDefault()
    setCreating(true)
    setError('')
    try {
      await api.createUser({ ...form, email: form.email.trim() || undefined, organizations: memberships })
      onCreated(form.username, memberships.length)
      close()
    } catch (reason) {
      setError(errorMessage(reason, 'Could not create the account.'))
      setCreating(false)
    }
  }

  return (
    <DialogFrame labelledBy="add-user-title" className="add-user-dialog" closing={closing} onDismiss={close} onSubmit={create}>
      <header className="use-model-header">
        <div>
          <span className="eyebrow">New account</span>
          <h2 id="add-user-title">Add a user</h2>
        </div>
        <button type="button" className="icon-button" onClick={close} aria-label="Close">
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
        {choices.length > 0 && (
          <fieldset className="add-user-orgs">
            <legend>Organizations <small>optional</small></legend>
            {memberships.map((item, index) => (
              <div className="add-user-org-row" key={index}>
                <select
                  value={item.organization}
                  onChange={(event) => changeMembership(index, { organization: event.target.value })}
                  aria-label={`Organization ${index + 1}`}
                >
                  {choices
                    .filter((choice) => choice.name === item.organization || !memberships.some((chosen) => chosen.organization === choice.name))
                    .map((choice) => (
                      <option key={choice.id} value={choice.name}>
                        {choice.display_name && choice.display_name !== choice.name ? `${choice.display_name} (${choice.name})` : choice.name}
                      </option>
                    ))}
                </select>
                <select
                  value={item.role}
                  onChange={(event) => changeMembership(index, { role: event.target.value as OrganizationRole })}
                  aria-label={`Role in ${item.organization}`}
                >
                  {(Object.keys(ORG_ROLE_LABELS) as OrganizationRole[]).map((role) => (
                    <option key={role} value={role}>{ORG_ROLE_LABELS[role]}</option>
                  ))}
                </select>
                <button
                  type="button"
                  className="icon-button"
                  onClick={() => setMemberships((current) => current.filter((_, position) => position !== index))}
                  aria-label={`Remove ${item.organization}`}
                >
                  <X size={16} />
                </button>
              </div>
            ))}
            {unused.length > 0 && (
              <button
                type="button"
                className="secondary-button compact add-user-org-add"
                onClick={() => setMemberships((current) => [...current, { organization: unused[0].name, role: 'read' }])}
              >
                <Building2 size={15} /> Add to an organization
              </button>
            )}
            {limitedByRole && (
              <p className="add-user-org-note">Viewers can only read, whatever their organization role.</p>
            )}
          </fieldset>
        )}
        {error && <div className="inline-error add-user-error">{error}</div>}
        <div className="add-user-footer">
          <span>They can change the password after signing in.</span>
          <button type="button" className="secondary-button" onClick={close}>Cancel</button>
          <button type="submit" className="download-button" disabled={creating}>
            {creating ? <LoaderCircle size={16} className="spin" /> : <UserPlus size={16} />} Create account
          </button>
        </div>
      </div>
    </DialogFrame>
  )
}

function UsersTab({ onToast }: { onToast: ToastHandler }) {
  const { user: me } = useAccess()
  const confirm = useConfirm()
  const location = useLocation()
  const { query: listQuery, update, search, setSearch, changePageSize } = useListQuery(
    USER_QUERY_DEFAULTS,
    'hugginghack.admin.users.per-page',
    USER_QUERY_CHOICES,
  )
  const query = listQuery as AdminUserQuery
  const [result, setResult] = useState<AdminUserPage | null>(null)
  const [loading, setLoading] = useState(true)
  const [accountsEnabled, setAccountsEnabled] = useState(true)
  const [busy, setBusy] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const latest = useRef(0)
  const users = result?.items || []

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

  async function remove(user: AdminUser) {
    const external = (user.auth_provider || 'local') !== 'local'
    const sure = await confirm({
      eyebrow: 'Delete account',
      title: `Delete ${user.username}?`,
      message: external ? (
        <>
          <p>The account, its sessions, and its API tokens are removed. This cannot be undone.</p>
          <p>It signs in through single sign-on, so it comes back the next time they sign in. To block access, disable it instead.</p>
        </>
      ) : (
        'The account, its sessions, and its API tokens are removed. This cannot be undone.'
      ),
      confirmLabel: 'Delete account',
      danger: true,
      requireText: user.username,
    })
    if (!sure) return
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
          {!result && loading && <RowSkeletons rows={6} cells={3} label="Loading" />}
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
                  <Link to={`/admin/users/${user.id}`} state={{ from: location.search }}>
                    <strong>{user.display_name}{self ? ' (you)' : ''}</strong>
                  </Link>
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
                  {accountsEnabled && (user.auth_provider || 'local') === 'local' && !self && (
                    <Link
                      to={`/admin/users/${user.id}`}
                      state={{ from: location.search }}
                      title="Change password"
                      aria-label={`Change password for ${user.username}`}
                    >
                      <KeyRound size={15} />
                    </Link>
                  )}
                  <button
                    type="button"
                    title="Sign out everywhere and revoke API tokens"
                    aria-label={`Revoke sessions and tokens for ${user.username}`}
                    onClick={async () => {
                      const sure = await confirm({
                        title: `Sign ${user.username} out everywhere?`,
                        message: 'Every browser signed in to this account has to sign in again, and all of its API tokens stop working.',
                        confirmLabel: 'Sign out and revoke',
                        danger: true,
                      })
                      if (sure) act(user, () => api.adminRevoke(user.id, { sessions: true, tokens: true }), `${user.username} was signed out everywhere.`)
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
        {result && (
          <ListPager
            page={result.page}
            pages={result.pages}
            perPage={result.per_page}
            total={result.total}
            label="Account pages"
            onPage={(page) => update({ page })}
            onPageSize={changePageSize}
          />
        )}
      </section>
      {adding && (
        <AddUserDialog
          onClose={() => setAdding(false)}
          onCreated={(username, organizations) => {
            onToast(
              organizations
                ? `${username} can now sign in and is in ${organizations} organization${organizations === 1 ? '' : 's'}.`
                : `${username} can now sign in.`,
            )
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
  if (!matrix) return <RowSkeletons rows={6} cells={3} label="Loading roles" />
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
  if (!server) return <RowSkeletons rows={6} cells={1} label="Loading the server configuration" />
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
            <Fact
              label="Site data"
              value={server.storage.system.location}
              good={server.storage.system.remote}
            />
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
            <Fact
              label="Server downloads"
              value={server.hugging_face.downloads_enabled ? 'On (API only)' : 'Off · models arrive by upload'}
            />
            {server.hugging_face.downloads_enabled && (
              <>
                <Fact label="Endpoint" value={server.hugging_face.endpoint} />
                <Fact label="Access token" value={server.hugging_face.token_configured ? 'Configured' : 'Not set'} good={server.hugging_face.token_configured} />
                <Fact label="Parallel downloads" value={`${server.hugging_face.max_concurrent_downloads} × ${server.hugging_face.workers_per_download} workers`} />
              </>
            )}
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

const ORG_SORT_LABELS: Record<AdminOrganizationSort, string> = {
  name: 'Name',
  newest: 'Newest',
  repositories: 'Most repositories',
  members: 'Most members',
}
const ORG_FILTERS: Array<{ id: AdminOrganizationFilter; label: string }> = [
  { id: '', label: 'All' },
  { id: 'with_repositories', label: 'With repositories' },
  { id: 'empty', label: 'No repositories' },
  { id: 'mine', label: 'You belong to' },
]
const ORG_QUERY_DEFAULTS = { q: '', filter: '', sort: 'name' }
const ORG_QUERY_CHOICES = {
  filter: ['with_repositories', 'empty', 'mine'],
  sort: Object.keys(ORG_SORT_LABELS),
}

function NewOrganizationDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (name: string) => void }) {
  const [form, setForm] = useState({ name: '', display_name: '', description: '' })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const firstField = useRef<HTMLInputElement>(null)
  const { closing, close } = useClosingTransition(onClose)

  useEffect(() => firstField.current?.focus(), [])

  async function create(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError('')
    try {
      const created = await api.createOrganization(form)
      onCreated(created.name)
      close()
    } catch (reason) {
      setError(errorMessage(reason, 'Could not create the organization.'))
      setSaving(false)
    }
  }

  return (
    <DialogFrame labelledBy="new-org-title" className="add-user-dialog" closing={closing} onDismiss={close} onSubmit={create}>
      <header className="use-model-header">
        <div>
          <span className="eyebrow">New organization</span>
          <h2 id="new-org-title">Create an organization</h2>
        </div>
        <button type="button" className="icon-button" onClick={close} aria-label="Close">
          <X size={20} />
        </button>
      </header>
      <div className="use-model-body">
        <div className="admin-create-user">
          <label>
            <span>Name</span>
            <input ref={firstField} value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="Nvidia" autoComplete="off" required />
          </label>
          <label>
            <span>Display name <small>optional</small></span>
            <input value={form.display_name} onChange={(event) => setForm({ ...form, display_name: event.target.value })} placeholder="NVIDIA" />
          </label>
          <div className="wide">
            <MarkdownEditor
              label="About (optional)"
              value={form.description}
              onChange={(description) => setForm({ ...form, description })}
              maxLength={10_000}
              rows={5}
              placeholder="What this organization publishes. Markdown works: **bold**, lists, links."
            />
          </div>
        </div>
        <p className="add-user-org-note new-org-hint">
          The name becomes a namespace, as in <code>{form.name.trim() || 'Nvidia'}/GLM-5.3-NVFP4</code>, and cannot be
          changed later. You become its first admin.
        </p>
        {error && <div className="inline-error add-user-error">{error}</div>}
        <div className="add-user-footer">
          <span>Add members from the organization page afterwards.</span>
          <button type="button" className="secondary-button" onClick={close}>Cancel</button>
          <button type="submit" className="download-button" disabled={saving}>
            {saving ? <LoaderCircle size={16} className="spin" /> : <Building2 size={16} />} Create organization
          </button>
        </div>
      </div>
    </DialogFrame>
  )
}

function OrganizationsTab({ onToast }: { onToast: ToastHandler }) {
  const confirm = useConfirm()
  const { query: listQuery, update, search, setSearch, changePageSize } = useListQuery(
    ORG_QUERY_DEFAULTS,
    'hugginghack.admin.organizations.per-page',
    ORG_QUERY_CHOICES,
  )
  const query = listQuery as AdminOrganizationQuery
  const [result, setResult] = useState<AdminOrganizationPage | null>(null)
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const latest = useRef(0)
  const items = result?.items || []

  const load = useCallback(() => {
    const request = ++latest.current
    setLoading(true)
    api
      .adminOrganizations(query)
      .then((payload) => {
        if (request !== latest.current) return
        setResult(payload)
        if (payload.page !== query.page) update({ page: payload.page }, true)
      })
      .catch((reason) => {
        if (request === latest.current) onToast(errorMessage(reason, 'Could not load organizations.'), 'error')
      })
      .finally(() => {
        if (request === latest.current) setLoading(false)
      })
  }, [query, onToast, update])

  useEffect(() => {
    load()
  }, [load])

  function clearFilters() {
    setSearch('')
    update({ q: '', filter: '' })
  }

  async function remove(organization: Organization) {
    const sure = await confirm({
      eyebrow: 'Delete organization',
      title: `Delete ${organization.display_name}?`,
      message: 'Its members lose access to it. An organization that still owns repositories cannot be deleted. This cannot be undone.',
      confirmLabel: 'Delete organization',
      danger: true,
      requireText: organization.name,
    })
    if (!sure) return
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
      <section className="settings-section admin-users">
        <div className="section-heading-line">
          <div>
            <span className="eyebrow">
              {result ? `${result.counts.all} organization${result.counts.all === 1 ? '' : 's'}${query.q ? ' match' : ''}` : 'Organizations'}
            </span>
            <h2>Organizations</h2>
          </div>
          <button type="button" className="download-button compact" onClick={() => setCreating(true)}>
            <Plus size={15} /> New organization
          </button>
        </div>
        <div className="admin-user-toolbar">
          <div className="catalog-search compact">
            <Search size={16} />
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search name or description"
              aria-label="Search organizations"
            />
            {search && (
              <button type="button" onClick={() => setSearch('')} aria-label="Clear search">
                <CircleX size={15} />
              </button>
            )}
          </div>
          <label className="sort-control compact">
            <SlidersHorizontal size={14} />
            <select
              value={query.sort}
              onChange={(event) => update({ sort: event.target.value as AdminOrganizationSort })}
              aria-label="Sort organizations"
            >
              {Object.entries(ORG_SORT_LABELS).map(([id, label]) => <option key={id} value={id}>Sort: {label}</option>)}
            </select>
            <ChevronDown size={14} />
          </label>
        </div>
        <div className="role-filter" role="group" aria-label="Filter organizations">
          {ORG_FILTERS.map((filter) => (
            <button
              key={filter.id || 'all'}
              type="button"
              className={query.filter === filter.id ? 'selected' : undefined}
              aria-pressed={query.filter === filter.id}
              onClick={() => update({ filter: filter.id })}
            >
              {filter.label}
              {result && <span>{result.counts[filter.id || 'all']}</span>}
            </button>
          ))}
        </div>
        <div className={loading && result ? 'admin-user-table refreshing' : 'admin-user-table'} role="table" aria-label="Organizations" aria-busy={loading}>
          <div className="admin-user-row org-row header" role="row">
            <span>Organization</span><span>Repositories</span><span>Members</span><span>Your role</span><span />
          </div>
          {!result && loading && <RowSkeletons rows={6} cells={3} label="Loading" />}
          {result && items.length === 0 && (
            <div className="admin-user-empty">
              {result.counts.all === 0 && !query.q ? (
                <>
                  <strong>No organizations yet.</strong>
                  <button type="button" className="secondary-button compact" onClick={() => setCreating(true)}>
                    <Plus size={14} /> New organization
                  </button>
                </>
              ) : (
                <>
                  <strong>No organizations match these filters.</strong>
                  <button type="button" className="secondary-button compact" onClick={clearFilters}>Clear filters</button>
                </>
              )}
            </div>
          )}
          {items.map((organization) => (
            <div className="admin-user-row org-row" role="row" key={organization.id}>
              <span className="admin-user-name">
                <Link to={`/orgs/${organization.name}`}><strong>{organization.display_name}</strong></Link>
                <small>@{organization.name}{organization.description ? ` · ${markdownSummary(organization.description, 90)}` : ''}</small>
              </span>
              <span className="admin-user-muted">{organization.repository_count || 0}</span>
              <span className="admin-user-muted">{organization.member_count || 0}</span>
              <span className="admin-user-muted">{organization.my_role ? ORG_ROLE_LABELS[organization.my_role] : '—'}</span>
              <span className="admin-user-actions">
                <Link to={`/orgs/${organization.name}/members`} className="secondary-button compact">Members</Link>
                <button type="button" className="danger-text" aria-label={`Delete ${organization.name}`} title="Delete organization" onClick={() => remove(organization)}>
                  <Trash2 size={15} />
                </button>
              </span>
            </div>
          ))}
        </div>
        {result && (
          <ListPager
            page={result.page}
            pages={result.pages}
            perPage={result.per_page}
            total={result.total}
            label="Organization pages"
            onPage={(page) => update({ page })}
            onPageSize={changePageSize}
          />
        )}
      </section>
      {creating && (
        <NewOrganizationDialog
          onClose={() => setCreating(false)}
          onCreated={(name) => {
            onToast(`${name} was created. You are its first admin.`)
            load()
          }}
        />
      )}
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
  const { tab: tabParam, userId } = useParams()
  // One account opens under the Users tab, which stays underlined.
  const tab = userId ? 'users' : tabParam
  const { can } = useAccess()
  const tabs = TABS.filter((item) => can(item.capability))
  const indicator = useTabIndicator<HTMLDivElement>(`${tab}:${tabs.length}`)
  const body = useFadeOnChange<HTMLDivElement>(userId ? `user:${userId}` : tab || '')
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
            <div ref={indicator}>
              {tabs.map((item) => (
                <NavLink key={item.id} to={`/admin/${item.id}`} className={({ isActive }) => (isActive ? 'active' : '')}>
                  {item.label}
                </NavLink>
              ))}
            </div>
          </nav>
        </div>
      </header>
      <div className="section-body admin-content" ref={body}>
        {active.id === 'users' && (userId ? <AdminUserDetail userId={userId} onToast={onToast} /> : <UsersTab onToast={onToast} />)}
        {active.id === 'roles' && <RolesTab />}
        {active.id === 'organizations' && <OrganizationsTab onToast={onToast} />}
        {active.id === 'storage' && <StoragePage onToast={onToast} />}
        {active.id === 'runtimes' && <RuntimesPage />}
        {active.id === 'server' && <ServerTab />}
      </div>
    </div>
  )
}
