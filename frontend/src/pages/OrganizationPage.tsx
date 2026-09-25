import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Box, Building2, LogOut, Pencil, Plus, Trash2, UploadCloud, Users } from 'lucide-react'
import { Link, NavLink, useNavigate, useParams } from 'react-router-dom'
import { useAccess } from '../access'
import { api } from '../api'
import { useFadeOnChange, useTabIndicator } from '../motion'
import { LibraryModelRow } from '../components/RepositoryRows'
import type { LibraryModel, Organization, OrganizationDetails, OrganizationRole } from '../types'
import { initials } from '../utils'
import { ModelCardSkeletons, RowSkeletons } from '../components/Skeletons'
import { useConfirm } from '../components/ConfirmDialog'
import { MarkdownEditor, MarkdownText } from '../components/Markdown'
import { markdownSummary } from '../markdownText'

type ToastHandler = (message: string, tone?: 'success' | 'error') => void

export const ORG_ROLE_LABELS: Record<OrganizationRole, string> = {
  admin: 'Admin',
  write: 'Write',
  read: 'Read',
}

const ORG_ROLE_HELP: Record<OrganizationRole, string> = {
  admin: 'Manage members and settings, change visibility, delete repositories',
  write: 'Create repositories, upload changes, and see private ones',
  read: 'See and pull repositories shared with the organization',
}

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback
}

export function OrganizationsIndex() {
  const [items, setItems] = useState<Organization[] | null>(null)
  useEffect(() => {
    api.organizations().then((payload) => setItems(payload.items)).catch(() => setItems([]))
  }, [])
  return (
    <div className="standard-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">Shared namespaces</span>
          <h1>Organizations</h1>
          <p>Teams and companies that publish models together, like nvidia/GLM-5.3-NVFP4.</p>
        </div>
      </div>
      {!items ? (
        <RowSkeletons rows={4} cells={2} label="Loading organizations" />
      ) : (
        <div className="org-grid">
          {items.map((organization) => (
            <Link key={organization.id} to={`/orgs/${organization.name}`} className="org-card">
              <span className="org-avatar">{initials(organization.name)}</span>
              <div>
                <strong>{organization.display_name}</strong>
                <small>@{organization.name}</small>
                <p>{markdownSummary(organization.description) || 'No description yet.'}</p>
                <span className="org-card-meta">
                  {organization.repository_count || 0} repositories · {organization.member_count || 0} members
                  {organization.my_role ? ` · you: ${ORG_ROLE_LABELS[organization.my_role]}` : ''}
                </span>
              </div>
            </Link>
          ))}
          {items.length === 0 && <div className="empty-compact">No organizations yet. Administrators create them under Admin → Organizations.</div>}
        </div>
      )}
    </div>
  )
}

function MembersTab({ organization, onChanged, onToast }: { organization: OrganizationDetails; onChanged: (value: OrganizationDetails | null) => void; onToast: ToastHandler }) {
  const { user } = useAccess()
  const confirm = useConfirm()
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [role, setRole] = useState<OrganizationRole>('write')
  const [busy, setBusy] = useState(false)

  async function run(action: () => Promise<OrganizationDetails>, message: string) {
    setBusy(true)
    try {
      onChanged(await action())
      onToast(message)
    } catch (reason) {
      onToast(errorMessage(reason, 'That change was not saved.'), 'error')
    } finally {
      setBusy(false)
    }
  }

  function add(event: FormEvent) {
    event.preventDefault()
    const name = username.trim().toLowerCase()
    if (!name) return
    run(() => api.setOrganizationMember(organization.name, name, role), `${name} can now ${role === 'read' ? 'see' : 'upload to'} ${organization.name}.`)
    setUsername('')
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
          <span className="eyebrow">{organization.members.length} members</span>
          <h2>Members</h2>
        </div>
        {organization.my_role && (
          <button type="button" className="secondary-button compact" onClick={leave}>
            <LogOut size={14} /> Leave
          </button>
        )}
      </div>
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
                  value={member.role}
                  disabled={busy}
                  aria-label={`Role for ${member.username}`}
                  onChange={(event) => run(() => api.setOrganizationMember(organization.name, member.username, event.target.value), `${member.username} is now ${ORG_ROLE_LABELS[event.target.value as OrganizationRole].toLowerCase()}.`)}
                >
                  {(Object.keys(ORG_ROLE_LABELS) as OrganizationRole[]).map((item) => (
                    <option key={item} value={item}>{ORG_ROLE_LABELS[item]}</option>
                  ))}
                </select>
                {member.id !== user.id && (
                  <button
                    type="button"
                    className="admin-user-actions-button danger-text"
                    aria-label={`Remove ${member.username}`}
                    title="Remove from organization"
                    onClick={() => run(() => api.removeOrganizationMember(organization.name, member.username), `${member.username} was removed.`)}
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
          <button className="download-button" disabled={busy}><Plus size={16} /> Add member</button>
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
  const [hardwareLabels, setHardwareLabels] = useState<Record<string, string>>({})
  const [error, setError] = useState('')
  const indicator = useTabIndicator<HTMLDivElement>(`${tab}:${organization?.name}:${organization?.can_manage}`)
  const body = useFadeOnChange<HTMLDivElement>(tab)

  const load = useCallback(() => {
    api.organization(name).then(setOrganization).catch((reason) => setError(reason.message))
    api
      .libraryModels(new URLSearchParams({ owner: name, sort: 'updated' }))
      .then((payload) => {
        setModels(payload.items)
        setHardwareLabels(Object.fromEntries(payload.facets.hardware.map(([id, label]) => [id, label])))
      })
      .catch(() => setModels([]))
  }, [name])

  useEffect(() => {
    load()
  }, [load])

  async function toggleSaved(model: LibraryModel) {
    try {
      if (model.saved) await api.unsaveModel(model.id)
      else await api.saveModel({ repo_id: model.id, metadata: { author: model.author, local: true } })
      setModels((current) => current?.map((item) => (item.id === model.id ? { ...item, saved: !model.saved } : item)) || null)
    } catch (reason) {
      onToast(errorMessage(reason, 'Could not update saved models.'), 'error')
    }
  }

  if (error) return <div className="standard-page"><div className="inline-error">{error}</div></div>
  if (!organization) return <div className="standard-page"><RowSkeletons rows={5} cells={2} label="Loading the organization" /></div>
  // The header shows the first line; the whole text, when there is more, opens the Models tab.
  const summary = markdownSummary(organization.description)
  const about = organization.description.trim() !== summary
  const tabs = [
    { id: 'models', label: 'Models', count: models?.length },
    { id: 'members', label: 'Members', count: organization.members.length },
    ...(organization.can_manage ? [{ id: 'settings', label: 'Settings' }] : []),
  ]

  return (
    <div className="section-page">
      <header className="section-hero">
        <div className="section-hero-inner">
          <div className="account-identity">
            <span className="account-avatar org-avatar-large" aria-hidden="true">{initials(organization.name)}</span>
            <div>
              <span className="eyebrow"><Building2 size={11} /> Organization</span>
              <h1>{organization.display_name}</h1>
              <p>
                @{organization.name}
                {organization.my_role && <> · <span className="role-badge member">You: {ORG_ROLE_LABELS[organization.my_role]}</span></>}
              </p>
              {summary && <p className="org-description">{summary}</p>}
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
        {tab === 'models' && about && (
          <section className="org-about" aria-label={`About ${organization.display_name}`}>
            <MarkdownText source={organization.description} />
          </section>
        )}
        {tab === 'models' && (
          models === null ? (
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
