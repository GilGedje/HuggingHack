import { useCallback, useEffect, useState, type FormEvent } from 'react'
import {
  Check,
  KeyRound,
  LoaderCircle,
  LogOut,
  Monitor,
  Palette,
  ShieldCheck,
  Trash2,
  UserCircle,
} from 'lucide-react'
import { Link, NavLink, Navigate, useParams } from 'react-router-dom'
import { useAccess } from '../access'
import { api } from '../api'
import { useFadeOnChange, useTabIndicator } from '../motion'
import { PasswordForm } from '../components/PasswordForm'
import { CopyButton } from '../components/UseModel'
import type { AccountOverview, AccountSession, ApiToken, StorageOption } from '../types'
import { resolveServerUrl } from '../useModel'
import { focusAfterRemoval } from '../focus'
import { THEME_EVENT, announceThemePreference, readThemePreference } from '../theme'
import { avatarUrl, describeDevice, relativeTime } from '../utils'
import { RowSkeletons } from '../components/Skeletons'
import { LoadError } from '../components/LoadError'
import { ORG_ROLE_LABELS, ROLE_LABELS, effectiveOrgRole } from '../roles'
import { useConfirm } from '../components/ConfirmDialog'
import { Avatar, AvatarEditor } from '../components/Avatar'

type ToastHandler = (message: string, tone?: 'success' | 'error') => void


function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback
}

function ProfileTab({ overview, onToast, onSaved }: { overview: AccountOverview; onToast: ToastHandler; onSaved: () => void }) {
  const { refresh } = useAccess()
  const [displayName, setDisplayName] = useState(overview.user.display_name)
  const [email, setEmail] = useState(overview.user.email || '')
  const [saving, setSaving] = useState(false)
  // Single sign-on accounts take their name and email from the directory at every sign-in.
  const external = (overview.user.auth_provider || 'local') !== 'local'
  const editable = overview.accounts_enabled && !external

  const [revokedTokens, setRevokedTokens] = useState(0)

  async function changePassword(next: string, current: string) {
    const result = await api.changePassword({ current_password: current, new_password: next })
    setRevokedTokens(result.api_tokens_revoked || 0)
  }

  async function save(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    try {
      await api.updateProfile({ display_name: displayName, email: email.trim() || null })
      onToast('Profile saved.')
      refresh()
      onSaved()
    } catch (reason) {
      onToast(errorMessage(reason, 'Could not save your profile.'), 'error')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="account-grid">
      <div className="account-column">
        <section className="settings-section">
          <div className="settings-section-title">
            <UserCircle size={20} />
            <div>
              <h2>Profile</h2>
              <p>How you appear on commits, uploads, and shared repositories.</p>
            </div>
          </div>
          <AvatarEditor
            name={overview.user.display_name || overview.user.username}
            label="your profile"
            src={avatarUrl(overview.user.username, overview.user.avatar_updated_at)}
            onUpload={async (picture) => {
              await api.uploadAvatar(picture)
              refresh()
              onSaved()
            }}
            onRemove={async () => {
              await api.deleteAvatar()
              refresh()
              onSaved()
            }}
            onToast={onToast}
          />
          <form className="account-form" onSubmit={save}>
            <label>
              Username
              <input value={overview.user.username} disabled />
            </label>
            <label>
              Display name
              <input value={displayName} onChange={(event) => setDisplayName(event.target.value)} maxLength={80} required disabled={!editable} />
            </label>
            <label>
              <span>Email {!external && <small>Optional</small>}</span>
              <input type="email" value={email} onChange={(event) => setEmail(event.target.value)} maxLength={254} disabled={!editable} />
            </label>
            {external ? (
              <p className="account-note">
                You sign in through your organization&apos;s directory, which keeps your name, email, and password.
                They update here each time you sign in.
              </p>
            ) : (
              <button className="download-button" disabled={saving || !editable}>
                {saving ? <LoaderCircle size={16} className="spin" /> : <Check size={16} />} Save profile
              </button>
            )}
          </form>
        </section>
        {overview.local_password && (
          <section className="settings-section">
            <div className="settings-section-title">
              <KeyRound size={20} />
              <div>
                <h2>Password</h2>
                <p>Changing it signs out your other sessions and revokes your API tokens.</p>
              </div>
            </div>
            <PasswordForm
              username={overview.user.username}
              askCurrent
              submitLabel="Change password"
              doneMessage={
                revokedTokens
                  ? `Password changed. Your other sessions were signed out and your ${revokedTokens === 1 ? 'API token was' : `${revokedTokens} API tokens were`} revoked; create new tokens where you still need them.`
                  : 'Password changed. Your other sessions were signed out.'
              }
              onSubmit={changePassword}
            />
          </section>
        )}
      </div>
      <section className="settings-section">
        <div className="settings-section-title">
          <ShieldCheck size={20} />
          <div>
            <h2>{ROLE_LABELS[overview.user.role]} access</h2>
            <p>
              {overview.user.role === 'admin' ? (
                <>
                  Your role decides what you can do. You change roles under{' '}
                  <Link to="/admin/users">Admin → Users</Link>; another administrator can change yours.
                </>
              ) : (
                'Your role decides what you can do. Ask an administrator to change it.'
              )}
            </p>
          </div>
        </div>
        <ul className="capability-list">
          {overview.capabilities.map((capability) => (
            <li key={capability.id}>
              <Check size={14} /> {capability.description}
            </li>
          ))}
        </ul>
        <dl className="settings-list account-facts">
          <div><dt>Member since</dt><dd>{relativeTime(overview.user.created_at)}</dd></div>
          <div><dt>Last sign-in</dt><dd>{overview.user.last_login_at ? relativeTime(overview.user.last_login_at) : 'Never'}</dd></div>
          <div><dt>Saved models</dt><dd><Link to="/saved">{overview.saved_count}</Link></dd></div>
          <div>
            <dt>Organizations</dt>
            <dd>
              {overview.organizations.length === 0 ? <Link to="/orgs">None</Link> : overview.organizations.map((organization) => (
                <Link key={organization.id} to={`/orgs/${organization.name}`} className="account-repo-link">
                  {organization.display_name} · {ORG_ROLE_LABELS[effectiveOrgRole(organization.role, overview.user.role)]}
                </Link>
              ))}
            </dd>
          </div>
          <div>
            <dt>Your repositories</dt>
            <dd>
              {overview.repositories.length === 0 ? 'None yet' : overview.repositories.map((repo) => (
                <Link key={repo} to={`/models/${repo}`} className="account-repo-link">{repo}</Link>
              ))}
            </dd>
          </div>
        </dl>
      </section>
    </div>
  )
}

function SecurityTab({ overview, onToast }: { overview: AccountOverview; onToast: ToastHandler }) {
  const confirm = useConfirm()
  const [sessions, setSessions] = useState<AccountSession[] | null>(null)
  const [loadError, setLoadError] = useState('')

  const load = useCallback(() => {
    if (!overview.accounts_enabled) return
    setLoadError('')
    api
      .sessions()
      .then((payload) => setSessions(payload.items))
      .catch((reason) => setLoadError(errorMessage(reason, 'The server did not answer.')))
  }, [overview.accounts_enabled])

  useEffect(() => {
    load()
  }, [load])

  async function revoke(session: AccountSession, trigger: HTMLElement) {
    const refocus = focusAfterRemoval(trigger)
    const sure = await confirm({
      title: `Sign out ${describeDevice(session.user_agent)}?`,
      message: 'That browser has to sign in again. Your API tokens keep working.',
      confirmLabel: 'Sign out',
    })
    if (!sure) return
    try {
      await api.revokeSession(session.id)
      onToast('That session was signed out.')
      refocus()
      load()
    } catch (reason) {
      onToast(errorMessage(reason, 'Could not sign out that session.'), 'error')
    }
  }

  async function revokeOthers() {
    const others = (sessions?.length || 1) - 1
    const sure = await confirm({
      title: `Sign out ${others === 1 ? 'your other session' : `all ${others} other sessions`}?`,
      message: 'Every other browser signed in to your account has to sign in again. This one stays signed in, and your API tokens keep working.',
      confirmLabel: 'Sign out others',
    })
    if (!sure) return
    try {
      await api.revokeOtherSessions()
      onToast('Every other session was signed out.')
      load()
    } catch (reason) {
      onToast(errorMessage(reason, 'Could not sign out other sessions.'), 'error')
    }
  }

  if (!overview.accounts_enabled) {
    return <div className="empty-compact">Accounts are disabled on this server, so there are no sessions to manage.</div>
  }

  return (
    <div className="account-single">
      <section className="settings-section">
        <div className="settings-section-title">
          <Monitor size={20} />
          <div>
            <h2>Active sessions</h2>
            <p>Browsers signed in to your account.</p>
          </div>
        </div>
        {loadError ? (
          <LoadError what="your sessions" message={loadError} onRetry={load} />
        ) : sessions === null ? (
          <RowSkeletons rows={2} cells={1} label="Loading your sessions" />
        ) : (
          <ul className="session-list">
            {sessions.map((session) => (
              <li key={session.id}>
                <div>
                  <strong>{describeDevice(session.user_agent)}</strong>
                  {session.current && <span className="local-badge">This browser</span>}
                  <small>
                    {session.ip || 'Unknown address'} · signed in {relativeTime(session.created_at)} · active {relativeTime(session.last_seen_at)}
                  </small>
                </div>
                {!session.current && (
                  <button type="button" className="secondary-button compact" onClick={(event) => revoke(session, event.currentTarget)}>
                    <LogOut size={14} /> Sign out
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
        {sessions && !loadError && sessions.length > 1 && (
          <button type="button" className="secondary-button" onClick={revokeOthers}>
            <LogOut size={15} /> Sign out all other sessions
          </button>
        )}
      </section>
    </div>
  )
}

const EXPIRY_OPTIONS: Array<[string, number | null]> = [
  ['30 days', 30],
  ['90 days', 90],
  ['1 year', 365],
  ['Never', null],
]

function TokensTab({ overview, onToast }: { overview: AccountOverview; onToast: ToastHandler }) {
  const { can } = useAccess()
  const confirm = useConfirm()
  const [tokens, setTokens] = useState<ApiToken[] | null>(null)
  const [loadError, setLoadError] = useState('')
  const [name, setName] = useState('')
  const [scope, setScope] = useState<'read' | 'write'>('read')
  // A write token can do no more than its owner, and viewers cannot change anything.
  const canWrite = can('repos.create') || can('repos.edit_own') || can('repos.edit_any')
  const [expiry, setExpiry] = useState('90 days')
  const [created, setCreated] = useState<ApiToken | null>(null)
  const [publicUrl, setPublicUrl] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const load = useCallback(() => {
    setLoadError('')
    api
      .tokens()
      .then((payload) => setTokens(payload.items))
      .catch((reason) => setLoadError(errorMessage(reason, 'The server did not answer.')))
  }, [])

  useEffect(() => {
    if (!overview.accounts_enabled) return
    load()
    api.health().then((health) => setPublicUrl(health.public_url || null)).catch(() => undefined)
  }, [load, overview.accounts_enabled])

  async function create(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    try {
      // null is a real choice ("Never"); only an unknown label falls back to 90 days.
      const option = EXPIRY_OPTIONS.find(([label]) => label === expiry)
      const expires = option ? option[1] : 90
      const token = await api.createToken({ name, scope: canWrite ? scope : 'read', expires_in_days: expires })
      setCreated(token)
      setName('')
      load()
    } catch (reason) {
      onToast(errorMessage(reason, 'Could not create the token.'), 'error')
    } finally {
      setSaving(false)
    }
  }

  async function revoke(token: ApiToken, trigger: HTMLElement) {
    const refocus = focusAfterRemoval(trigger)
    const sure = await confirm({
      title: `Revoke “${token.name}”?`,
      message: 'Anything that uses it stops working immediately. This cannot be undone.',
      confirmLabel: 'Revoke token',
      danger: true,
    })
    if (!sure) return
    try {
      await api.deleteToken(token.id)
      onToast(`“${token.name}” was revoked.`)
      if (created?.id === token.id) setCreated(null)
      refocus()
      load()
    } catch (reason) {
      onToast(errorMessage(reason, 'Could not revoke the token.'), 'error')
    }
  }

  if (!overview.accounts_enabled) {
    return <div className="empty-compact">Accounts are disabled on this server; API tokens are not needed.</div>
  }
  if (!can('tokens.manage')) {
    return <div className="empty-compact">Your role cannot create API tokens.</div>
  }
  const server = resolveServerUrl(publicUrl, window.location.origin)
  const host = server.replace(/^https?:\/\//, '')
  const scheme = server.startsWith('https') ? 'https' : 'http'

  return (
    <div className="tokens-tab">
      <section className="settings-section">
        <div className="settings-section-title">
          <KeyRound size={20} />
          <div>
            <h2>Create an API token</h2>
            <p>Tokens let scripts, vLLM, git, and the hf CLI act as you, including your private repositories.</p>
          </div>
        </div>
        <form className="token-form" onSubmit={create}>
          <label>
            Name
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder="GPU server" maxLength={80} required />
          </label>
          <label>
            Access
            <select
              value={canWrite ? scope : 'read'}
              onChange={(event) => setScope(event.target.value as 'read' | 'write')}
              aria-describedby={canWrite ? undefined : 'token-read-only'}
            >
              <option value="read">Read: browse and pull models</option>
              <option value="write" disabled={!canWrite}>Write: also upload and change repositories</option>
            </select>
            {!canWrite && <small id="token-read-only">Your role can only read, so its tokens are read-only.</small>}
          </label>
          <label>
            Expires
            <select value={expiry} onChange={(event) => setExpiry(event.target.value)}>
              {EXPIRY_OPTIONS.map(([label]) => <option key={label}>{label}</option>)}
            </select>
          </label>
          <button className="download-button" disabled={saving}>
            {saving ? <LoaderCircle size={16} className="spin" /> : <KeyRound size={16} />} Create token
          </button>
        </form>
        {created?.token && (
          <div className="new-token">
            <strong>Copy your new token now. It will not be shown again.</strong>
            <div className="snippet-code new-token-value">
              <pre><code>{created.token}</code></pre>
              <CopyButton text={created.token} label="Copy token" />
            </div>
            <p>Use it anywhere Hugging Face tokens work:</p>
            <div className="snippet-code">
              <pre><code>{`export HF_ENDPOINT="${server}"\nexport HF_TOKEN="${created.token}"\nhf download owner/model\n\ngit config --global credential.helper cache\n# If git asks for a password, paste the token\ngit clone ${scheme}://${overview.user.username}@${host}/owner/model`}</code></pre>
              <CopyButton text={`export HF_ENDPOINT="${server}"\nexport HF_TOKEN="${created.token}"`} label="Copy environment variables" />
            </div>
          </div>
        )}
      </section>
      <section className="settings-section">
        <div className="settings-section-title">
          <ShieldCheck size={20} />
          <div>
            <h2>Your tokens</h2>
            <p>Revoke any token you no longer use.</p>
          </div>
        </div>
        {loadError ? (
          <LoadError what="your tokens" message={loadError} onRetry={load} />
        ) : tokens === null ? (
          <RowSkeletons rows={2} cells={1} label="Loading your tokens" />
        ) : (
          <ul className="token-list">
            {tokens.map((token) => (
              <li key={token.id}>
                <div>
                  <strong>{token.name}</strong>
                  <code>{token.prefix}…</code>
                  <span className={token.scope === 'write' ? 'change-tag modified' : 'change-tag added'}>{token.scope}</span>
                  <small>
                    Created {relativeTime(token.created_at)} · last used {token.last_used_at ? relativeTime(token.last_used_at) : 'never'} ·{' '}
                    {token.expires_at ? `expires ${relativeTime(token.expires_at)}` : 'never expires'}
                  </small>
                </div>
                <button type="button" className="secondary-button compact danger-text" onClick={(event) => revoke(token, event.currentTarget)}>
                  <Trash2 size={14} /> Revoke
                </button>
              </li>
            ))}
            {tokens.length === 0 && <li className="empty-compact">No tokens yet.</li>}
          </ul>
        )}
      </section>
    </div>
  )
}

function PreferencesTab({ overview, onToast }: { overview: AccountOverview; onToast: ToastHandler }) {
  const { can, refresh } = useAccess()
  const [preferences, setPreferences] = useState(overview.user.preferences || {})

  // The header's theme button changes the same preference; the menu follows it.
  useEffect(() => {
    const follow = (event: Event) => {
      const theme = readThemePreference((event as CustomEvent<unknown>).detail)
      if (theme) setPreferences((current) => ({ ...current, theme }))
    }
    window.addEventListener(THEME_EVENT, follow)
    return () => window.removeEventListener(THEME_EVENT, follow)
  }, [])
  const [targets, setTargets] = useState<StorageOption[]>([])

  useEffect(() => {
    if (can('repos.create')) {
      api.storageOptions().then((payload) => setTargets(payload.items)).catch(() => undefined)
    }
  }, [can])

  async function save(key: 'theme' | 'catalog_sort' | 'default_storage_target', value: string) {
    try {
      const updated = await api.updatePreferences({ [key]: value || null })
      setPreferences(updated)
      if (key === 'theme') announceThemePreference(readThemePreference(value) || 'system')
      refresh()
      onToast('Preference saved.')
    } catch (reason) {
      onToast(errorMessage(reason, 'Could not save the preference.'), 'error')
    }
  }

  return (
    <section className="settings-section preferences">
      <div className="settings-section-title">
        <Palette size={20} />
        <div>
          <h2>Preferences</h2>
          <p>Saved to your account, so they follow you to any browser.</p>
        </div>
      </div>
      <div className="account-form">
        <label>
          Theme
          <select value={preferences.theme || 'system'} onChange={(event) => save('theme', event.target.value)}>
            <option value="system">Match this device</option>
            <option value="light">Light</option>
            <option value="dark">Dark</option>
          </select>
        </label>
        <label>
          Default model sort
          <select value={preferences.catalog_sort || 'updated'} onChange={(event) => save('catalog_sort', event.target.value)}>
            <option value="updated">Recently updated</option>
            <option value="name">Name</option>
            <option value="size">Largest on disk</option>
            <option value="parameters">Most parameters</option>
          </select>
        </label>
        {can('repos.create') && targets.length > 1 && (
          <label>
            Default storage for new uploads
            <select value={preferences.default_storage_target || ''} onChange={(event) => save('default_storage_target', event.target.value)}>
              <option value="">Server default</option>
              {targets.map((target) => <option key={target.id} value={target.id}>{target.name}</option>)}
            </select>
          </label>
        )}
      </div>
    </section>
  )
}

const TABS = [
  { id: 'profile', label: 'Profile' },
  { id: 'security', label: 'Security' },
  { id: 'tokens', label: 'API tokens' },
  { id: 'preferences', label: 'Preferences' },
]

export function AccountPage({ onToast }: { onToast: ToastHandler }) {
  const { tab = 'profile' } = useParams()
  const { user } = useAccess()
  const [overview, setOverview] = useState<AccountOverview | null>(null)
  const [error, setError] = useState('')
  const indicator = useTabIndicator<HTMLDivElement>(tab)
  const body = useFadeOnChange<HTMLDivElement>(tab)

  const load = useCallback(() => {
    setError('')
    api.account().then(setOverview).catch((reason) => setError(errorMessage(reason, 'The server did not answer.')))
  }, [])

  useEffect(() => {
    load()
  }, [load])

  if (!TABS.some((item) => item.id === tab)) return <Navigate to="/account" replace />

  return (
    <div className="section-page">
      <header className="section-hero">
        <div className="section-hero-inner">
          <div className="account-identity">
            <span className="account-avatar" aria-hidden="true">
              <Avatar name={user.display_name || user.username} src={avatarUrl(user.username, user.avatar_updated_at)} />
            </span>
            <div>
              <span className="eyebrow">Your account</span>
              <h1>{user.display_name}</h1>
              <p>
                @{user.username} · <span className={`role-badge ${user.role}`}>{ROLE_LABELS[user.role]}</span>
              </p>
            </div>
          </div>
          <nav className="model-tabs" aria-label="Account sections">
            <div ref={indicator}>
              {TABS.map((item) => (
                <NavLink key={item.id} to={item.id === 'profile' ? '/account' : `/account/${item.id}`} end className={({ isActive }) => (isActive ? 'active' : '')}>
                  {item.label}
                </NavLink>
              ))}
            </div>
          </nav>
        </div>
      </header>
      <div className="section-body" ref={body}>
        {error && <LoadError what="your account" message={error} onRetry={load} />}
        {!overview && !error && (
          <RowSkeletons rows={4} cells={1} label="Loading your account" />
        )}
        {overview && tab === 'profile' && <ProfileTab overview={overview} onToast={onToast} onSaved={load} />}
        {overview && tab === 'security' && <SecurityTab overview={overview} onToast={onToast} />}
        {overview && tab === 'tokens' && <TokensTab overview={overview} onToast={onToast} />}
        {overview && tab === 'preferences' && <PreferencesTab overview={overview} onToast={onToast} />}
      </div>
    </div>
  )
}
