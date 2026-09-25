import { useCallback, useEffect, useState } from 'react'
import {
  ArrowLeft,
  ChevronDown,
  KeyRound,
  LoaderCircle,
  LogOut,
  Monitor,
  ShieldCheck,
  Trash2,
  UserCheck,
  UserCircle,
  UserX,
} from 'lucide-react'
import { Link, useLocation } from 'react-router-dom'
import { useAccess } from '../access'
import { api } from '../api'
import type { AdminUserDetail as Detail, ApiToken, Role } from '../types'
import { avatarUrl, describeDevice, relativeTime } from '../utils'
import { PasswordForm } from './PasswordForm'
import { useConfirm } from './ConfirmDialog'
import { Avatar } from './Avatar'
import { ROLE_LABELS, disableConfirmation, roleConfirmation } from '../roles'
import { focusAfterRemoval } from '../focus'
import { LoadError } from './LoadError'
import { RowSkeletons } from './Skeletons'

type ToastHandler = (message: string, tone?: 'success' | 'error') => void

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback
}

/** One account on the admin page: who they are, how they sign in, and what can act for them. */
export function AdminUserDetail({ userId, onToast }: { userId: string; onToast: ToastHandler }) {
  const { user: me } = useAccess()
  const confirm = useConfirm()
  const location = useLocation()
  // The list's search, filters, and page come back with the Back link.
  const from = (location.state as { from?: string } | null)?.from || ''
  const [detail, setDetail] = useState<Detail | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    setError('')
    api.adminUser(userId).then(setDetail).catch((reason) => setError(errorMessage(reason, 'The server did not answer.')))
  }, [userId])

  useEffect(() => {
    load()
  }, [load])

  /** Resolves true when the change was saved. */
  async function act(action: () => Promise<unknown>, message: string): Promise<boolean> {
    setBusy(true)
    try {
      await action()
      onToast(message)
      load()
      return true
    } catch (reason) {
      onToast(errorMessage(reason, 'That change was not saved.'), 'error')
      return false
    } finally {
      setBusy(false)
    }
  }

  const back = (
    <Link className="quiet-link admin-user-back" to={`/admin/users${from}`}>
      <ArrowLeft size={14} /> All users
    </Link>
  )
  if (error) {
    return (
      <>
        {back}
        <LoadError what="this account" message={error} onRetry={load} />
      </>
    )
  }
  if (!detail) {
    return (
      <>
        {back}
        <RowSkeletons rows={5} cells={2} label="Loading the account" />
      </>
    )
  }

  const { user } = detail
  const self = user.id === me.id

  async function changeRole(role: Role) {
    if (!(await confirm(roleConfirmation(user.username, user.role, role)))) return
    act(() => api.adminUpdateUser(user.id, { role }), `${user.username} is now ${ROLE_LABELS[role].toLowerCase()}.`)
  }

  async function toggleDisabled() {
    // Enabling gives nothing away, so only disabling asks first.
    if (!user.disabled && !(await confirm(disableConfirmation(user.username)))) return
    act(
      () => api.adminUpdateUser(user.id, { disabled: !user.disabled }),
      `${user.username} was ${user.disabled ? 'enabled' : 'disabled'}.`,
    )
  }

  async function setPassword(next: string) {
    const tokens = detail?.tokens.length || 0
    const sure = await confirm({
      eyebrow: 'Set a new password',
      title: `Replace ${user.username}'s password?`,
      message: tokens
        ? `They are signed out everywhere, and ${tokens === 1 ? 'their API token is' : `all ${tokens} of their API tokens are`} revoked, so scripts and pulls that use ${tokens === 1 ? 'it' : 'them'} need a new one.`
        : 'They are signed out everywhere and sign in again with the new password.',
      confirmLabel: 'Set password',
      danger: true,
    })
    // Declining keeps what was typed.
    if (!sure) return false
    await api.adminResetPassword(user.id, next)
    load()
  }

  async function revokeToken(token: ApiToken, trigger: HTMLElement) {
    const refocus = focusAfterRemoval(trigger)
    const sure = await confirm({
      title: `Revoke “${token.name}”?`,
      message: `${token.prefix}… stops working at once for anything that uses it. This cannot be undone.`,
      confirmLabel: 'Revoke token',
      danger: true,
    })
    if (!sure) return
    if (await act(() => api.adminRevokeToken(user.id, token.id), `“${token.name}” was revoked.`)) refocus()
  }

  return (
    <div className="admin-user-detail">
      {back}
      <section className="settings-section admin-user-card">
        <span className="account-avatar" aria-hidden="true">
          <Avatar name={user.display_name || user.username} src={avatarUrl(user.username, user.avatar_updated_at)} />
        </span>
        <div>
          <h2>{user.display_name}{self ? ' (you)' : ''}</h2>
          <p>
            @{user.username} · <span className={`role-badge ${user.role}`}>{ROLE_LABELS[user.role]}</span>{' '}
            <span className={user.disabled ? 'status-pill danger' : 'status-pill ok'}>{user.disabled ? 'Disabled' : 'Active'}</span>
          </p>
          <small>
            {detail.external
              ? `Single sign-on (${user.auth_provider}) · name, email, and password are managed by the directory`
              : detail.accounts_enabled
                ? 'Local account · signs in with a HuggingHack password'
                : 'Accounts are off on this server; everyone uses this account without signing in'}
          </small>
        </div>
        {busy && <LoaderCircle size={16} className="spin" />}
      </section>

      <div className="account-grid">
        <div className="account-column">
          <section className="settings-section">
            <div className="settings-section-title">
              <UserCircle size={20} />
              <div>
                <h2>Account</h2>
                <p>Role and access are decided here, whichever way they sign in.</p>
              </div>
            </div>
            <dl className="settings-list account-facts">
              <div><dt>Email</dt><dd>{user.email || '—'}</dd></div>
              <div>
                <dt>Role</dt>
                <dd>
                  <label className="sort-control compact">
                    <select
                      value={user.role}
                      disabled={self || busy}
                      aria-label={`Role for ${user.username}`}
                      onChange={(event) => changeRole(event.target.value as Role)}
                    >
                      {Object.entries(ROLE_LABELS).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
                    </select>
                    <ChevronDown size={14} />
                  </label>
                </dd>
              </div>
              <div><dt>Member since</dt><dd>{relativeTime(user.created_at)}</dd></div>
              <div><dt>Last sign-in</dt><dd>{user.last_login_at ? relativeTime(user.last_login_at) : 'Never'}</dd></div>
              <div>
                <dt>Organizations</dt>
                <dd>
                  {detail.organizations.length === 0 ? 'None' : detail.organizations.map((organization) => (
                    <Link key={organization.id} to={`/orgs/${organization.name}`} className="account-repo-link">
                      {organization.display_name} · {organization.role}
                    </Link>
                  ))}
                </dd>
              </div>
              <div>
                <dt>Repositories</dt>
                <dd>
                  {detail.repositories.length === 0 ? 'None' : detail.repositories.map((repo) => (
                    <Link key={repo} to={`/models/${repo}`} className="account-repo-link">{repo}</Link>
                  ))}
                </dd>
              </div>
            </dl>
            {!self && (
              <div className="admin-user-detail-actions">
                <button
                  type="button"
                  className="secondary-button compact"
                  disabled={busy}
                  onClick={toggleDisabled}
                >
                  {user.disabled ? <UserCheck size={14} /> : <UserX size={14} />}
                  {user.disabled ? 'Enable account' : 'Disable account'}
                </button>
              </div>
            )}
          </section>

          {detail.accounts_enabled && (
            <section className="settings-section">
              <div className="settings-section-title">
                <KeyRound size={20} />
                <div>
                  <h2>Password</h2>
                  <p>
                    {detail.external
                      ? 'This account has no HuggingHack password.'
                      : 'Set a new one when they are locked out. They are signed out everywhere and their API tokens are revoked.'}
                  </p>
                </div>
              </div>
              {detail.external ? (
                <p className="account-note">They sign in through the directory; reset their password there.</p>
              ) : self ? (
                <p className="account-note">
                  Change your own password from <Link to="/account">your Profile</Link>, where it asks for the current one.
                </p>
              ) : (
                <PasswordForm
                  askCurrent={false}
                  submitLabel="Set new password"
                  doneMessage={`${user.username}'s password was changed. They were signed out everywhere and their API tokens were revoked.`}
                  onSubmit={setPassword}
                />
              )}
            </section>
          )}
        </div>

        <div className="account-column">
          <section className="settings-section">
            <div className="settings-section-title">
              <ShieldCheck size={20} />
              <div>
                <h2>API tokens</h2>
                <p>Only the first characters are kept, enough to match a token someone reports.</p>
              </div>
            </div>
            {detail.accounts_enabled ? (
              <>
                <ul className="token-list">
                  {detail.tokens.map((token) => (
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
                      <button type="button" className="secondary-button compact danger-text" disabled={busy} onClick={(event) => revokeToken(token, event.currentTarget)}>
                        <Trash2 size={14} /> Revoke
                      </button>
                    </li>
                  ))}
                  {detail.tokens.length === 0 && <li className="empty-compact">No API tokens.</li>}
                </ul>
                {detail.tokens.length > 1 && (
                  <button
                    type="button"
                    className="secondary-button compact danger-text"
                    disabled={busy}
                    onClick={async () => {
                      const sure = await confirm({
                        title: `Revoke all of ${user.username}'s API tokens?`,
                        message: `All ${detail.tokens.length} stop working at once. This cannot be undone.`,
                        confirmLabel: 'Revoke all tokens',
                        danger: true,
                      })
                      if (sure) act(() => api.adminRevoke(user.id, { sessions: false, tokens: true }), `${user.username}'s tokens were revoked.`)
                    }}
                  >
                    <Trash2 size={14} /> Revoke all tokens
                  </button>
                )}
              </>
            ) : (
              <p className="account-note">Accounts are disabled on this server, so there are no tokens.</p>
            )}
          </section>

          {detail.accounts_enabled && (
            <section className="settings-section">
              <div className="settings-section-title">
                <Monitor size={20} />
                <div>
                  <h2>Sessions</h2>
                  <p>Browsers signed in to this account.</p>
                </div>
              </div>
              <ul className="session-list">
                {detail.sessions.map((session) => (
                  <li key={session.id}>
                    <div>
                      <strong>{describeDevice(session.user_agent)}</strong>
                      <small>
                        {session.ip || 'Unknown address'} · signed in {relativeTime(session.created_at)} · active {relativeTime(session.last_seen_at)}
                      </small>
                    </div>
                  </li>
                ))}
                {detail.sessions.length === 0 && <li className="empty-compact">Not signed in anywhere.</li>}
              </ul>
              {detail.sessions.length > 0 && !self && (
                <button
                  type="button"
                  className="secondary-button compact"
                  disabled={busy}
                  onClick={async () => {
                    const sure = await confirm({
                      title: `Sign ${user.username} out everywhere?`,
                      message: 'Every browser signed in to this account has to sign in again. API tokens keep working.',
                      confirmLabel: 'Sign out everywhere',
                    })
                    if (sure) act(() => api.adminRevoke(user.id, { sessions: true, tokens: false }), `${user.username} was signed out everywhere.`)
                  }}
                >
                  <LogOut size={14} /> Sign out everywhere
                </button>
              )}
            </section>
          )}
        </div>
      </div>
    </div>
  )
}
