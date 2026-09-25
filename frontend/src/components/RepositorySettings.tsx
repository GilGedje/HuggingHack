import { useEffect, useState, type FormEvent } from 'react'
import { AlertTriangle, Cloud, Globe2, HardDrive, LoaderCircle, LockKeyhole, Trash2, Users } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useAccess } from '../access'
import { api } from '../api'
import type { LibraryModelDetails, UploadNamespace, Visibility } from '../types'
import { VISIBILITIES, visibilityAllowed, visibilityAudience, visibilityLabel } from '../visibility'
import { ChoiceCard } from './ChoiceCard'
import { NamespacePicker } from './NamespacePicker'

type ToastHandler = (message: string, tone?: 'success' | 'error') => void

const SLUG_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$/
const VISIBILITY_ICONS = { private: LockKeyhole, organization: Users, public: Globe2 }

function message(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback
}

function announce(repoId: string) {
  window.dispatchEvent(new CustomEvent('hugginghack:repository-changed', { detail: repoId }))
}

/** Rename, transfer, visibility, description, and deletion for one repository. */
export function RepositorySettings({
  model,
  onChanged,
  onToast,
}: {
  model: LibraryModelDetails
  onChanged: () => void
  onToast: ToastHandler
}) {
  const { can } = useAccess()
  const navigate = useNavigate()
  const [owner, name] = model.id.split('/')
  const organization = model.organization?.name || null

  const [description, setDescription] = useState(model.description)
  const [savingDescription, setSavingDescription] = useState(false)
  const [visibility, setVisibility] = useState<Visibility>(model.visibility)
  const [savingVisibility, setSavingVisibility] = useState(false)

  const [namespaces, setNamespaces] = useState<UploadNamespace[]>([])
  const [namespace, setNamespace] = useState(owner)
  const [newName, setNewName] = useState(name)
  const [renameConfirm, setRenameConfirm] = useState('')
  const [renaming, setRenaming] = useState(false)
  const [renameError, setRenameError] = useState('')

  const [deleteConfirm, setDeleteConfirm] = useState('')
  const [deleting, setDeleting] = useState(false)

  useEffect(() => {
    setDescription(model.description)
    setVisibility(model.visibility)
    setNamespace(model.id.split('/')[0])
    setNewName(model.id.split('/')[1])
    setRenameConfirm('')
    setDeleteConfirm('')
  }, [model.id, model.description, model.visibility])

  // Owners it can move to: yourself and organizations you write to, all
  // organizations for administrators, and the current owner to rename in place.
  useEffect(() => {
    let ignore = false
    Promise.all([
      api.uploadNamespaces().then((payload) => payload.items).catch(() => []),
      can('storage.manage') ? api.organizations().then((payload) => payload.items).catch(() => []) : [],
    ]).then(([own, organizations]) => {
      if (ignore) return
      const choices: UploadNamespace[] = [...own]
      for (const item of organizations) {
        if (!choices.some((choice) => choice.name.toLowerCase() === item.name.toLowerCase())) {
          choices.push({ name: item.name, kind: 'organization', display_name: item.display_name })
        }
      }
      const current = model.id.split('/')[0]
      if (!choices.some((choice) => choice.name === current)) {
        choices.unshift({
          name: current,
          kind: model.organization ? 'organization' : 'user',
          display_name: model.organization?.display_name || current,
        })
      }
      setNamespaces(choices)
    })
    return () => {
      ignore = true
    }
  }, [can, model.id, model.organization])

  const target = `${namespace}/${newName.trim()}`
  const nameProblem = newName.trim() && !SLUG_PATTERN.test(newName.trim())
    ? 'Use letters, numbers, dots, underscores, or hyphens, starting with a letter or number.'
    : ''
  const unchanged = target === model.id
  const transfer = namespace.toLowerCase() !== owner.toLowerCase()
  const remote = model.storage_backend === 's3'

  async function saveDescription(event: FormEvent) {
    event.preventDefault()
    setSavingDescription(true)
    try {
      await api.updateUploadRepository(model.id, { description, visibility: model.visibility })
      onChanged()
      onToast('Description saved.')
    } catch (reason) {
      onToast(message(reason, 'Could not save the description.'), 'error')
    } finally {
      setSavingDescription(false)
    }
  }

  async function changeVisibility(next: Visibility) {
    const previous = visibility
    setVisibility(next)
    setSavingVisibility(true)
    try {
      await api.updateUploadRepository(model.id, { description: model.description, visibility: next })
      onChanged()
      announce(model.id)
      onToast(`${model.id} is now ${visibilityLabel(next).toLowerCase()}.`)
    } catch (reason) {
      setVisibility(previous)
      onToast(message(reason, 'Could not change visibility.'), 'error')
    } finally {
      setSavingVisibility(false)
    }
  }

  async function rename(event: FormEvent) {
    event.preventDefault()
    setRenaming(true)
    setRenameError('')
    try {
      const result = await api.renameRepository(model.id, {
        namespace,
        name: newName.trim(),
        confirmation: renameConfirm,
      })
      announce(result.repo_id)
      onToast(transfer ? `Moved to ${result.repo_id}.` : `Renamed to ${result.repo_id}.`)
      navigate(`/models/${result.repo_id}/settings`, { replace: true })
    } catch (reason) {
      setRenameError(message(reason, 'Could not rename the repository.'))
    } finally {
      setRenaming(false)
    }
  }

  async function remove(event: FormEvent) {
    event.preventDefault()
    setDeleting(true)
    try {
      await api.deleteRepository(model.id, deleteConfirm)
      announce(model.id)
      onToast(`${model.id} and its files were deleted.`)
      navigate('/models', { replace: true })
    } catch (reason) {
      onToast(message(reason, 'Could not delete the repository.'), 'error')
      setDeleting(false)
    }
  }

  return (
    <div className="repo-settings">
      {model.owned ? (
        <>
          <section className="settings-block">
            <div className="settings-block-heading">
              <h2>Visibility</h2>
              <p>Who can find, open, and pull this model.</p>
            </div>
            <fieldset className="choice-group" disabled={savingVisibility}>
              <legend className="sr-only">Visibility</legend>
              {VISIBILITIES.map((option) => {
                const Icon = VISIBILITY_ICONS[option]
                return (
                  <ChoiceCard
                    key={option}
                    name="repo-visibility"
                    checked={visibility === option}
                    disabled={!visibilityAllowed(option, organization)}
                    onChange={() => changeVisibility(option)}
                    icon={<Icon size={17} />}
                    title={visibilityLabel(option)}
                  >
                    {visibilityAudience(option, organization)}
                  </ChoiceCard>
                )
              })}
            </fieldset>
          </section>

          <form className="settings-block" onSubmit={saveDescription}>
            <div className="settings-block-heading">
              <h2>Description</h2>
              <p>A line shown on the Uploads page and organization pages.</p>
            </div>
            <textarea
              aria-label="Description"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              maxLength={500}
            />
            <div className="settings-block-actions">
              <button
                className="secondary-button compact"
                disabled={savingDescription || description === model.description}
              >
                {savingDescription && <LoaderCircle size={14} className="spin" />} Save description
              </button>
            </div>
          </form>
        </>
      ) : (
        <section className="settings-block">
          <div className="settings-block-heading">
            <h2>Visibility</h2>
            <p>
              This model was downloaded or found in storage, so it has no owner and every account can
              see it. Move it to a user or organization below to control who sees it.
            </p>
          </div>
        </section>
      )}

      <form className="settings-block" onSubmit={rename}>
        <div className="settings-block-heading">
          <h2>Rename or transfer</h2>
          <p>Change the name, or move the model to yourself or an organization you write to.</p>
        </div>
        {remote ? (
          <p className="settings-note">Models stored in S3 cannot be renamed yet.</p>
        ) : (
          <>
            <div className="repo-name-grid">
              <label htmlFor="rename-owner">Owner</label>
              <span aria-hidden="true" />
              <label htmlFor="new-repo-name">Model name</label>
              {namespaces.length > 0 && (
                <NamespacePicker id="rename-owner" namespaces={namespaces} value={namespace} onChange={setNamespace} />
              )}
              <span className="repo-name-slash" aria-hidden="true">/</span>
              <input
                id="new-repo-name"
                value={newName}
                onChange={(event) => setNewName(event.target.value)}
                autoComplete="off"
                spellCheck={false}
                aria-invalid={Boolean(nameProblem) || undefined}
              />
            </div>
            {nameProblem && <p className="field-hint problem">{nameProblem}</p>}
            {!unchanged && !nameProblem && newName.trim() && (
              <div className="settings-warning">
                <AlertTriangle size={15} />
                <div>
                  <strong>
                    {model.id} becomes {target}
                  </strong>
                  <p>
                    Old links, <code>vllm serve {model.id}</code>, and git remotes stop working; there is no
                    redirect. Files, commit history, saves, and hardware tags move with it.
                    {transfer && !model.owned && ' It becomes owned and stays public until you change that.'}
                  </p>
                </div>
              </div>
            )}
            {!unchanged && !nameProblem && newName.trim() && (
              <label className="wizard-label settings-confirm">
                <span>
                  Type <code>{model.id}</code> to confirm
                </span>
                <input
                  value={renameConfirm}
                  onChange={(event) => setRenameConfirm(event.target.value)}
                  autoComplete="off"
                  spellCheck={false}
                />
              </label>
            )}
            {renameError && <div className="inline-error"><AlertTriangle size={16} /> {renameError}</div>}
            <div className="settings-block-actions">
              <button
                className="secondary-button compact"
                disabled={unchanged || Boolean(nameProblem) || !newName.trim() || renameConfirm !== model.id || renaming}
              >
                {renaming && <LoaderCircle size={14} className="spin" />}
                {transfer ? 'Transfer' : 'Rename'}
              </button>
            </div>
          </>
        )}
      </form>

      <section className="settings-block">
        <div className="settings-block-heading">
          <h2>Storage</h2>
          <p>Where the files live. Moving between locations is not available yet.</p>
        </div>
        <p className="settings-storage">
          {remote ? <Cloud size={15} /> : <HardDrive size={15} />} {model.storage_target_name}
          <code>{model.remote_uri || model.local_path}</code>
        </p>
      </section>

      <form className="settings-block danger" onSubmit={remove}>
        <div className="settings-block-heading">
          <h2>Delete this model</h2>
          <p>Removes every file from storage, its commit history, and its hardware tags. This cannot be undone.</p>
        </div>
        <label className="wizard-label settings-confirm">
          <span>
            Type <code>{model.id}</code> to confirm
          </span>
          <input
            value={deleteConfirm}
            onChange={(event) => setDeleteConfirm(event.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
        </label>
        <div className="settings-block-actions">
          <button className="danger-button compact" disabled={deleteConfirm !== model.id || deleting}>
            {deleting ? <LoaderCircle size={14} className="spin" /> : <Trash2 size={14} />} Delete model
          </button>
        </div>
      </form>
    </div>
  )
}
