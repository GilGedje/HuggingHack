import { useCallback, useEffect, useState, type FormEvent } from 'react'
import {
  Check,
  KeyRound,
  LoaderCircle,
  LogOut,
  Minus,
  Plus,
  ShieldAlert,
  Trash2,
  UserCheck,
  UserX,
  Wifi,
} from 'lucide-react'
import { NavLink, Navigate, useParams } from 'react-router-dom'
import { useAccess } from '../access'
import { api } from '../api'
import type { AdminUser, PermissionMatrix, Role, ServerSettings } from '../types'
import { relativeTime } from '../utils'
import { RuntimesPage } from './RuntimesPage'
import { StoragePage } from './StoragePage'

type ToastHandler = (message: string, tone?: 'success' | 'error') => void

const ROLE_LABELS: Record<Role, string> = { admin: 'Administrator', member: 'Member', viewer: 'Viewer' }

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback
}

function UsersTab({ onToast }: { onToast: ToastHandler }) {
  const { user: me } = useAccess()
  const [users, setUsers] = useState<AdminUser[]>([])
  const [accountsEnabled, setAccountsEnabled] = useState(true)
  const [busy, setBusy] = useState<string | null>(null)
  const [form, setForm] = useState({ username: '', display_name: '', email: '', password: '', role: 'member' as Role })
  const [creating, setCreating] = useState(false)

  const load = useCallback(() => {
    api
      .adminUsers()
      .then((payload) => {
        setUsers(payload.items)
        setAccountsEnabled(payload.accounts_enabled)
      })
      .catch((reason) => onToast(errorMessage(reason, 'Could not load accounts.'), 'error'))
  }, [onToast])

  useEffect(() => {
    load()
  }, [load])

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
    if (window.prompt(`Type ${user.username} to delete this account permanently.`) !== user.username) return
    act(user, () => api.adminDeleteUser(user.id), `${user.username} was deleted.`)
  }

  async function create(event: FormEvent) {
    event.preventDefault()
    setCreating(true)
    try {
      await api.createUser({ ...form, email: form.email.trim() || undefined })
      onToast(`${form.username} can now sign in.`)
      setForm({ username: '', display_name: '', email: '', password: '', role: 'member' })
      load()
    } catch (reason) {
      onToast(errorMessage(reason, 'Could not create the account.'), 'error')
    } finally {
      setCreating(false)
    }
  }

  return (
    <>
      <section className="settings-section admin-users">
        <div className="section-heading-line">
          <div>
            <span className="eyebrow">{users.length} account{users.length === 1 ? '' : 's'}</span>
            <h2>Users</h2>
          </div>
        </div>
        <div className="admin-user-table" role="table" aria-label="Accounts">
          <div className="admin-user-row header" role="row">
            <span>Account</span><span>Role</span><span>Status</span><span>Last sign-in</span><span>Access</span><span />
          </div>
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
      </section>
      {accountsEnabled && (
        <section className="settings-section">
          <div className="settings-section-title">
            <Plus size={20} />
            <div>
              <h2>Add a user</h2>
              <p>They can change their password after signing in.</p>
            </div>
          </div>
          <form className="admin-create-user" onSubmit={create}>
            <label>Username<input value={form.username} onChange={(event) => setForm({ ...form, username: event.target.value })} required /></label>
            <label>Display name<input value={form.display_name} onChange={(event) => setForm({ ...form, display_name: event.target.value })} /></label>
            <label>Email <small>optional</small><input type="email" value={form.email} onChange={(event) => setForm({ ...form, email: event.target.value })} /></label>
            <label>
              Role
              <select value={form.role} onChange={(event) => setForm({ ...form, role: event.target.value as Role })}>
                {Object.entries(ROLE_LABELS).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
              </select>
            </label>
            <label>Temporary password<input type="password" autoComplete="new-password" minLength={12} value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} required /></label>
            <button className="download-button" disabled={creating}>
              {creating ? <LoaderCircle size={16} className="spin" /> : <Plus size={16} />} Create account
            </button>
          </form>
        </section>
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

const TABS = [
  { id: 'users', label: 'Users', capability: 'users.manage' },
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
        {active.id === 'storage' && <StoragePage onToast={onToast} />}
        {active.id === 'runtimes' && <RuntimesPage />}
        {active.id === 'server' && <ServerTab />}
      </div>
    </div>
  )
}
