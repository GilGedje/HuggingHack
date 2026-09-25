import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { Box, Building2, LogOut, Pencil, Plus, Trash2, UploadCloud, Users } from 'lucide-react'
import { Link, NavLink, useNavigate, useParams } from 'react-router-dom'
import { useAccess } from '../access'
import { api } from '../api'
import { useFadeOnChange, useTabIndicator } from '../motion'
import { LibraryModelRow } from '../components/RepositoryRows'
import type { LibraryModel, Organization, OrganizationDetails, OrganizationMember, OrganizationRole } from '../types'
import { avatarUrl, countLabel } from '../utils'
import { ModelCardSkeletons, RowSkeletons } from '../components/Skeletons'
import { useConfirm } from '../components/ConfirmDialog'
import { LoadError } from '../components/LoadError'
import { MarkdownEditor, MarkdownText } from '../components/Markdown'
import { markdownSummary } from '../markdownText'
import { focusAfterRemoval } from '../focus'
import { Avatar, AvatarEditor } from '../components/Avatar'
import { ORG_ROLE_LABELS, ROLE_LABELS, actingOrgAdmins, effectiveOrgRole, orgRoleConfirmation } from '../roles'

type ToastHandler = (message: string, tone?: 'success' | 'error') => void

export { ORG_ROLE_LABELS }

const ORG_ROLE_HELP: Record<OrganizationRole, string> = {
  admin: 'Manage members and settings, change visibility, delete repositories',
  write: 'Create repositories, upload changes, and see private ones',
  read: 'See and pull repositories shared with the organization',
}

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback
}

export function OrganizationsIndex() {
  const { user } = useAccess()
  const [items, setItems] = useState<Organization[] | null>(null)
  const [error, setError] = useState('')
  const load = useCallback(() => {
    setError('')
    api
      .organizations()
      .then((payload) => setItems(payload.items))
      .catch((reason) => setError(errorMessage(reason, 'The server did not answer.')))
  }, [])
  useEffect(() => {
    load()
  }, [load])
  return (
    <div className="section-page">
      <header className="section-hero">
        <div className="section-hero-inner">
          <span className="eyebrow"><Building2 size={11} /> Shared namespaces</span>
          <h1>Organizations</h1>
          <p>Teams and companies that publish models together, like nvidia/GLM-5.3-NVFP4.</p>
        </div>
      </header>
      <div className="section-body">
        {error ? (
          <LoadError what="organizations" message={error} onRetry={load} />
        ) : !items ? (
          <RowSkeletons rows={4} cells={2} label="Loading organizations" />
        ) : (
          <div className="org-grid">
            {items.map((organization) => (
              <Link key={organization.id} to={`/orgs/${organization.name}`} className="org-card">
                <span className="org-avatar"><Avatar name={organization.name} src={avatarUrl(organization.name, organization.avatar_updated_at)} /></span>
                <div>
                  <strong>{organization.display_name}</strong>
                  <small>@{organization.name}</small>
                  <p>{markdownSummary(organization.description) || 'No description yet.'}</p>
                  <span className="org-card-meta">
                    {countLabel(organization.repository_count || 0, 'repository', 'repositories')} ·{' '}
                    {countLabel(organization.member_count || 0, 'member', 'members')}
                    {organization.my_role ? ` · you: ${ORG_ROLE_LABELS[effectiveOrgRole(organization.my_role, user.role)]}` : ''}
                  </span>
                </div>
              </Link>
            ))}
            {items.length === 0 && <div className="empty-compact">No organizations yet. Administrators create them under Admin → Organizations.</div>}
          </div>
        )}
      </div>
    </div>
  )
}

/** The role name for someone whose server role narrows their organization role. */
function RoleLimitNote({ role, serverRole }: { role: OrganizationRole; serverRole?: OrganizationMember['server_role'] }) {
  if (effectiveOrgRole(role, serverRole) === role) return null
  return <> · {ORG_ROLE_LABELS[role]} role, but {ROLE_LABELS.viewer} accounts on this server can only read</>
}

function MembersTab({ organization, onChanged, onToast }: { organization: OrganizationDetails; onChanged: (value: OrganizationDetails | null) => void; onToast: ToastHandler }) {
  const { user } = useAccess()
  const confirm = useConfirm()
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [role, setRole] = useState<OrganizationRole>('write')
  const [busy, setBusy] = useState(false)
  // The role a select shows while its question is open; declining puts the old one back.
  const [pending, setPending] = useState<{ username: string; role: OrganizationRole } | null>(null)

  // The only admin cannot step down or leave; someone must be able to manage it.
  // Disabled accounts and server Viewers keep an Admin row but cannot act on it.
  const me = organization.members.find((member) => member.id === user.id)
  const lastAdmin =
    !!me && me.role === 'admin' && effectiveOrgRole(me.role, user.role) === 'admin' && actingOrgAdmins(organization.members) <= 1

  /** Resolves true when the change was saved. */
  async function run(
    action: () => Promise<OrganizationDetails>,
    message: string | ((saved: OrganizationDetails) => string),
  ): Promise<boolean> {
    setBusy(true)
    try {
      const saved = await action()
      onChanged(saved)
      onToast(typeof message === 'string' ? message : message(saved))
      return true
    } catch (reason) {
      onToast(errorMessage(reason, 'That change was not saved.'), 'error')
      return false
    } finally {
      setBusy(false)
    }
  }

  /** What an added member can now do, and why they might not yet. */
  function joined(name: string, saved: OrganizationDetails): string {
    const member = saved.members.find((item) => item.username === name)
    const acting = member ? effectiveOrgRole(member.role, member.server_role) : role
    const can = acting === 'read' ? 'see' : 'upload to'
    if (member?.disabled) return `${name} was added to ${saved.display_name}; their account is disabled, so they cannot sign in until it is enabled.`
    return `${name} can now ${can} ${saved.display_name}.`
  }

  async function changeRole(member: OrganizationMember, next: OrganizationRole) {
    setPending({ username: member.username, role: next })
    try {
      if (!(await confirm(orgRoleConfirmation(member.username, organization.display_name, member.role, next)))) return
      await run(
        () => api.setOrganizationMember(organization.name, member.username, next),
        `${member.username} is now ${ORG_ROLE_LABELS[next]} in ${organization.display_name}.`,
      )
    } finally {
      setPending(null)
    }
  }

  async function add(event: FormEvent) {
    event.preventDefault()
    const name = username.trim().toLowerCase()
    if (!name) return
    const existing = organization.members.find((member) => member.username === name)
    if (existing && existing.role === role) {
      onToast(`${name} is already a member, as ${ORG_ROLE_LABELS[role]}.`, 'error')
      return
    }
    // Adding someone as an admin, or changing an existing member's role, asks first.
    if ((existing || role === 'admin') && !(await confirm(orgRoleConfirmation(name, organization.display_name, existing?.role || null, role)))) return
    // The name stays put if the change fails, so a typo is quick to fix.
    const saved = await run(
      () => api.setOrganizationMember(organization.name, name, role),
      existing ? `${name} is now ${ORG_ROLE_LABELS[role]} in ${organization.display_name}.` : (value) => joined(name, value),
    )
    if (saved) setUsername('')
  }

  async function remove(username: string, trigger: HTMLElement) {
    const refocus = focusAfterRemoval(trigger)
    const sure = await confirm({
      eyebrow: 'Remove member',
      title: `Remove ${username} from ${organization.display_name}?`,
      message: `They lose access to every ${organization.display_name} repository that is not public, including uploading to it. An admin can add them again.`,
      confirmLabel: 'Remove member',
      danger: true,
    })
    if (sure && (await run(() => api.removeOrganizationMember(organization.name, username), `${username} was removed.`))) refocus()
  }

  async function leave() {
    const sure = await confirm({
      title: `Leave ${organization.display_name}?`,
      message: 'You lose access to its private repositories. An admin of the organization can add you again.',
      confirmLabel: 'Leave organization',
      danger: true,
    })
    if (!sure) return
    try {
      await api.removeOrganizationMember(organization.name, user.username)
      onToast(`You left ${organization.display_name}.`)
      onChanged(null)
      navigate('/orgs')
    } catch (reason) {
      onToast(errorMessage(reason, 'Could not leave the organization.'), 'error')
    }
  }

  return (
    <section className="settings-section">
      <div className="section-heading-line">
        <div>
          <span className="eyebrow">{countLabel(organization.members.length, 'member', 'members')}</span>
          <h2>Members</h2>
        </div>
        {organization.my_role && (
          <button
            type="button"
            className="secondary-button compact"
            onClick={leave}
            disabled={lastAdmin}
            aria-describedby={lastAdmin ? 'org-last-admin' : undefined}
          >
            <LogOut size={14} /> Leave
          </button>
        )}
      </div>
      {lastAdmin && (
        <p className="field-hint org-last-admin" id="org-last-admin">
          You are the only admin. Make another member an admin before you step down or leave.
        </p>
      )}
      <ul className="org-role-help">
        {(Object.keys(ORG_ROLE_LABELS) as OrganizationRole[]).map((item) => (
          <li key={item}><strong>{ORG_ROLE_LABELS[item]}</strong> {ORG_ROLE_HELP[item]}</li>
        ))}
      </ul>
      <ul className="session-list org-members">
        {organization.members.map((member) => (
          <li key={member.id}>
            <div>
              <strong>{member.display_name}{member.id === user.id ? ' (you)' : ''}</strong>
              <small>
                @{member.username}
                {member.server_role === 'viewer' && member.role !== 'read' ? ' · server viewer: can read but not upload' : ''}
                {member.disabled ? ' · account disabled' : ''}
              </small>
            </div>
            {organization.can_manage ? (
              <span className="org-member-actions">
                <select
                  value={pending?.username === member.username ? pending.role : member.role}
                  disabled={busy || pending !== null}
                  aria-label={`Role for ${member.username}`}
                  aria-describedby={member.id === user.id && lastAdmin ? 'org-last-admin' : undefined}
                  onChange={(event) => changeRole(member, event.target.value as OrganizationRole)}
                >
                  {(Object.keys(ORG_ROLE_LABELS) as OrganizationRole[]).map((item) => (
                    <option
                      key={item}
                      value={item}
                      // Viewers on the server can only read; the server refuses more.
                      disabled={(member.id === user.id && lastAdmin && item !== 'admin') || (member.server_role === 'viewer' && item !== 'read' && item !== member.role)}
                    >
                      {ORG_ROLE_LABELS[item]}
                    </option>
                  ))}
                </select>
                {member.id !== user.id && (
                  <button
                    type="button"
                    className="admin-user-actions-button danger-text"
                    aria-label={`Remove ${member.username}`}
                    title="Remove from organization"
                    onClick={(event) => remove(member.username, event.currentTarget)}
                  >
                    <Trash2 size={15} />
                  </button>
                )}
              </span>
            ) : (
              <span className={`role-badge ${member.role === 'admin' ? 'admin' : member.role === 'write' ? 'member' : ''}`}>{ORG_ROLE_LABELS[member.role]}</span>
            )}
          </li>
        ))}
      </ul>
      {organization.can_manage && (
        <form className="org-add-member" onSubmit={add}>
          <label>
            Username
            <input value={username} onChange={(event) => setUsername(event.target.value)} placeholder="jane-doe" required />
          </label>
          <label>
            Role
            <select value={role} onChange={(event) => setRole(event.target.value as OrganizationRole)}>
              {(Object.keys(ORG_ROLE_LABELS) as OrganizationRole[]).map((item) => (
                <option key={item} value={item}>{ORG_ROLE_LABELS[item]}</option>
              ))}
            </select>
          </label>
          <button className="download-button" disabled={busy || !username.trim()}><Plus size={16} /> Add member</button>
        </form>
      )}
    </section>
  )
}

function SettingsTab({ organization, onChanged, onToast }: { organization: OrganizationDetails; onChanged: (value: OrganizationDetails) => void; onToast: ToastHandler }) {
  const [displayName, setDisplayName] = useState(organization.display_name)
  const [description, setDescription] = useState(organization.description)
  async function save(event: FormEvent) {
    event.preventDefault()
    try {
      onChanged(await api.updateOrganization(organization.name, { display_name: displayName, description }))
      onToast('Organization saved.')
    } catch (reason) {
      onToast(errorMessage(reason, 'Could not save the organization.'), 'error')
    }
  }
  return (
    <section className="settings-section">
      <div className="settings-section-title">
        <Pencil size={20} />
        <div>
          <h2>Profile</h2>
          <p>The name <code>{organization.name}</code> is permanent because repositories live under it.</p>
        </div>
      </div>
      <AvatarEditor
        name={organization.name}
        label={organization.display_name}
        src={avatarUrl(organization.name, organization.avatar_updated_at)}
        onUpload={async (picture) => {
          const { avatar_updated_at } = await api.uploadOrganizationAvatar(organization.name, picture)
          onChanged({ ...organization, avatar_updated_at })
        }}
        onRemove={async () => {
          await api.deleteOrganizationAvatar(organization.name)
          onChanged({ ...organization, avatar_updated_at: null })
        }}
        onToast={onToast}
      />
      <form className="account-form" onSubmit={save}>
        <label>Display name<input value={displayName} onChange={(event) => setDisplayName(event.target.value)} maxLength={80} required /></label>
        <MarkdownEditor
          label="About"
          value={description}
          onChange={setDescription}
          maxLength={10_000}
          placeholder={'What this organization publishes, who it is for, and where to find more.\n\n**Bold**, _italic_, headings, lists, and links all work.'}
        />
        <button className="download-button">Save</button>
      </form>
    </section>
  )
}

export function OrganizationPage({ onToast }: { onToast: ToastHandler }) {
  const { name = '', tab = 'models' } = useParams()
  const navigate = useNavigate()
  const [organization, setOrganization] = useState<OrganizationDetails | null>(null)
  const [models, setModels] = useState<LibraryModel[] | null>(null)
  // Others' models made from this organization's: quantizations, fine-tunes, and so on.
  const [builtOn, setBuiltOn] = useState<LibraryModel[]>([])
  const [hardwareLabels, setHardwareLabels] = useState<Record<string, string>>({})
  const [error, setError] = useState('')
  const [modelsError, setModelsError] = useState('')
  const { user } = useAccess()
  const indicator = useTabIndicator<HTMLDivElement>(`${tab}:${organization?.name}:${organization?.can_manage}`)
  const body = useFadeOnChange<HTMLDivElement>(tab)

  // Only the latest load may answer: a slow reply for another organization, or
  // for an earlier load of this one, is dropped.
  const latest = useRef(0)
  const load = useCallback(() => {
    const request = ++latest.current
    const current = () => request === latest.current
    api
      .organization(name)
      .then((value) => {
        if (!current()) return
        setOrganization(value)
        setError('')
      })
      .catch((reason) => {
        if (current()) setError(errorMessage(reason, 'The server did not answer.'))
      })
    setModelsError('')
    api
      .libraryModels(new URLSearchParams({ owner: name, sort: 'updated' }))
      .then((payload) => {
        if (!current()) return
        setModels(payload.items)
        setHardwareLabels(Object.fromEntries(payload.facets.hardware.map(([id, label]) => [id, label])))
      })
      .catch((reason) => {
        if (current()) setModelsError(errorMessage(reason, 'The server did not answer.'))
      })
    api
      .libraryModels(new URLSearchParams({ built_on: name, sort: 'updated' }))
      .then((payload) => {
        if (current()) setBuiltOn(payload.items)
      })
      .catch(() => {
        if (current()) setBuiltOn([])
      })
  }, [name])

  // Another organization starts from nothing, so the last one never shows under its name.
  useEffect(() => {
    setOrganization(null)
    setModels(null)
    setBuiltOn([])
    setError('')
    load()
  }, [load])

  async function toggleSaved(model: LibraryModel) {
    try {
      if (model.saved) await api.unsaveModel(model.id)
      else await api.saveModel({ repo_id: model.id, metadata: { author: model.author, local: true } })
      const flip = (item: LibraryModel) => (item.id === model.id ? { ...item, saved: !model.saved } : item)
      setModels((current) => current?.map(flip) || null)
      setBuiltOn((current) => current.map(flip))
      onToast(model.saved ? `${model.id} was removed from your saved library.` : `${model.id} was saved for later.`)
    } catch (reason) {
      onToast(errorMessage(reason, 'Could not update saved models.'), 'error')
    }
  }

  if (error) {
    return (
      <div className="standard-page">
        <LoadError what="this organization" message={error} onRetry={() => { setError(''); load() }} />
        <p className="account-note"><Link className="quiet-link" to="/orgs">All organizations</Link></p>
      </div>
    )
  }
  if (!organization) return <div className="standard-page"><RowSkeletons rows={5} cells={2} label="Loading the organization" /></div>
  const tabs = [
    { id: 'models', label: 'Models', count: modelsError ? undefined : models?.length },
    ...(builtOn.length ? [{ id: 'built-on', label: `Built on ${organization.display_name}`, count: builtOn.length }] : []),
    { id: 'members', label: 'Members', count: organization.members.length },
    ...(organization.can_manage ? [{ id: 'settings', label: 'Settings' }] : []),
  ]

  return (
    <div className="section-page">
      <header className="section-hero">
        <div className="section-hero-inner">
          <div className="account-identity">
            <span className="account-avatar org-avatar-large" aria-hidden="true">
              <Avatar name={organization.name} src={avatarUrl(organization.name, organization.avatar_updated_at)} />
            </span>
            <div>
              <span className="eyebrow"><Building2 size={11} /> Organization</span>
              <h1>{organization.display_name}</h1>
              <p>
                @{organization.name}
                {organization.my_role && (
                  <>
                    {' · '}
                    <span className="role-badge member">You: {ORG_ROLE_LABELS[effectiveOrgRole(organization.my_role, user.role)]}</span>
                    <RoleLimitNote role={organization.my_role} serverRole={user.role} />
                  </>
                )}
              </p>
              {organization.description.trim() && (
                <section className="org-about" aria-label={`About ${organization.display_name}`}>
                  <MarkdownText source={organization.description} />
                </section>
              )}
            </div>
          </div>
          <nav className="model-tabs" aria-label="Organization sections">
            <div ref={indicator}>
              {tabs.map((item) => (
                <NavLink key={item.id} to={item.id === 'models' ? `/orgs/${organization.name}` : `/orgs/${organization.name}/${item.id}`} end className={({ isActive }) => (isActive ? 'active' : '')}>
                  {item.label}
                  {item.count != null && <span>{item.count}</span>}
                </NavLink>
              ))}
            </div>
            {organization.can_upload && (
              <div className="model-tab-actions">
                <button type="button" className="download-button compact" onClick={() => navigate(`/uploads?namespace=${encodeURIComponent(organization.name)}`)}>
                  <UploadCloud size={15} /> Upload a model
                </button>
              </div>
            )}
          </nav>
        </div>
      </header>
      <div className="section-body" ref={body}>
        {tab === 'models' && (
          modelsError ? (
            <LoadError what="this organization’s models" message={modelsError} onRetry={load} />
          ) : models === null ? (
            <ModelCardSkeletons count={4} label="Loading the organization's models" />
          ) : models.length ? (
            <div className="model-card-grid">
              {models.map((model) => (
                <LibraryModelRow
                  key={model.id}
                  model={model}
                  onOpen={(repoId) => navigate(`/models/${repoId}`)}
                  onUse={(item) => navigate(`/models/${item.id}?${item.apps.includes('vllm') ? 'local-app=vllm' : 'clone=true'}`)}
                  onSave={toggleSaved}
                  hardwareLabels={hardwareLabels}
                />
              ))}
            </div>
          ) : (
            <div className="empty-state">
              <Box size={28} />
              <h2>No models yet</h2>
              <p>{organization.can_upload ? 'Upload the first model for this organization.' : 'Models published by this organization appear here.'}</p>
            </div>
          )
        )}
        {tab === 'built-on' && (
          <>
            <p className="org-built-on-note">
              Quantizations, fine-tunes, and adapters that other publishers made from {organization.display_name}’s models.
            </p>
            <div className="model-card-grid">
              {builtOn.map((model) => (
                <LibraryModelRow
                  key={model.id}
                  model={model}
                  onOpen={(repoId) => navigate(`/models/${repoId}`)}
                  onUse={(item) => navigate(`/models/${item.id}?${item.apps.includes('vllm') ? 'local-app=vllm' : 'clone=true'}`)}
                  onSave={toggleSaved}
                  hardwareLabels={hardwareLabels}
                />
              ))}
            </div>
          </>
        )}
        {tab === 'members' && (
          <MembersTab
            organization={organization}
            onChanged={(value) => (value ? setOrganization(value) : load())}
            onToast={onToast}
          />
        )}
        {tab === 'settings' && organization.can_manage && (
          <SettingsTab organization={organization} onChanged={setOrganization} onToast={onToast} />
        )}
        {tab === 'members' && !organization.can_manage && (
          <p className="account-note"><Users size={13} /> Organization admins manage who belongs here.</p>
        )}
      </div>
    </div>
  )
}
