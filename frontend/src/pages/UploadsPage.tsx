import { useCallback, useEffect, useRef, useState } from 'react'
import { Globe2, LockKeyhole, RotateCcw, Settings, Trash2, UploadCloud, Users } from 'lucide-react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api'
import { UploadWizard } from '../components/UploadWizard'
import type { OwnedRepository, UploadNamespace, User, Visibility } from '../types'
import { formatBytes, relativeTime } from '../utils'
import { VISIBILITIES, visibilityAllowed, visibilityAudience, visibilityConfirmation, visibilityLabel } from '../visibility'
import { RowSkeletons } from '../components/Skeletons'
import { useConfirm } from '../components/ConfirmDialog'

type ToastHandler = (message: string, tone?: 'success' | 'error') => void

const VISIBILITY_ICONS = { private: LockKeyhole, organization: Users, public: Globe2 }

function RepositoryRow({
  repository,
  onResume,
  onChanged,
  onToast,
}: {
  repository: OwnedRepository
  onResume: () => void
  onChanged: () => Promise<void>
  onToast: ToastHandler
}) {
  const Icon = VISIBILITY_ICONS[repository.visibility] || LockKeyhole
  const ready = repository.status === 'ready'
  const organization = repository.organization_name || null
  const confirm = useConfirm()
  const [pendingVisibility, setPendingVisibility] = useState<Visibility | null>(null)

  async function changeVisibility(visibility: Visibility) {
    // The choice shows while the question is open; declining puts the old one back.
    setPendingVisibility(visibility)
    try {
      if (!(await confirm(visibilityConfirmation(repository.repo_id, visibility, organization)))) return
      await api.updateUploadRepository(repository.repo_id, { description: repository.description, visibility })
      await onChanged()
      onToast(`${repository.repo_id} is now ${visibilityLabel(visibility).toLowerCase()}.`)
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Unable to update repository', 'error')
    } finally {
      setPendingVisibility(null)
    }
  }

  async function remove() {
    const sure = await confirm({
      eyebrow: 'Delete repository',
      title: `Delete ${repository.repo_id}?`,
      message: 'Its files are removed from model storage, along with its commit history and deployment configs. This cannot be undone.',
      confirmLabel: 'Delete repository',
      danger: true,
      requireText: repository.repo_id,
    })
    if (!sure) return
    try {
      await api.deleteUploadRepository(repository.repo_id, repository.repo_id)
      onChanged()
      onToast(`${repository.repo_id} and its files were deleted.`)
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Unable to delete repository', 'error')
    }
  }

  return (
    <article className="owned-repository">
      <div className="repository-icon" title={visibilityAudience(repository.visibility, organization)}>
        <Icon size={18} />
      </div>
      <div>
        <small>{repository.organization_name || repository.owner_display_name}</small>
        <h3>
          {ready ? <Link to={`/models/${repository.repo_id}`}>{repository.repo_id}</Link> : repository.repo_id}
        </h3>
        {repository.description && <p>{repository.description}</p>}
        <div className="repo-stats">
          {ready ? (
            <span className="status-pill ok">{visibilityLabel(repository.visibility)}</span>
          ) : (
            <span className="status-pill pending">Waiting for files</span>
          )}
          {repository.missing && (
            <span className="status-pill danger" title="The last scan found none of its files in storage. Copy them back and rescan, or delete the repository.">
              Files missing
            </span>
          )}
          {repository.size_bytes != null && <span>{formatBytes(repository.size_bytes)}</span>}
          {repository.file_count != null && <span>{repository.file_count} files</span>}
          <span>Updated {relativeTime(repository.updated_at)}</span>
        </div>
      </div>
      <div className="repository-actions">
        {ready ? (
          <Link to={`/models/${repository.repo_id}?upload=1`}>
            <UploadCloud size={14} /> Upload changes
          </Link>
        ) : (
          <button onClick={onResume}>
            <RotateCcw size={14} /> Resume
          </button>
        )}
        {repository.my_role === 'admin' && (
          <>
            {ready && (
              <Link to={`/models/${repository.repo_id}/settings`}>
                <Settings size={14} /> Settings
              </Link>
            )}
            {ready && (
              <select
                className="visibility-select"
                aria-label={`Visibility of ${repository.repo_id}`}
                value={pendingVisibility || repository.visibility}
                onChange={(event) => changeVisibility(event.target.value as Visibility)}
              >
                {VISIBILITIES.filter((option) => visibilityAllowed(option, organization)).map((option) => (
                  <option key={option} value={option}>{visibilityLabel(option)}</option>
                ))}
              </select>
            )}
            <button className="danger-text" onClick={remove}>
              <Trash2 size={14} /> Delete
            </button>
          </>
        )}
      </div>
    </article>
  )
}

export function UploadsPage({ user, onToast }: { user: User; onToast: ToastHandler }) {
  const [searchParams] = useSearchParams()
  const [repositories, setRepositories] = useState<OwnedRepository[]>([])
  const [namespaces, setNamespaces] = useState<UploadNamespace[]>([])
  const [resume, setResume] = useState<OwnedRepository | null>(null)
  const [wizardKey, setWizardKey] = useState(0)
  const [loaded, setLoaded] = useState(false)
  const wizard = useRef<HTMLDivElement>(null)

  const load = useCallback(async () => {
    try {
      const [repos, spaces] = await Promise.all([api.uploadRepositories(), api.uploadNamespaces()])
      setRepositories(repos.items)
      setNamespaces(spaces.items)
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Unable to load repositories', 'error')
    } finally {
      setLoaded(true)
    }
  }, [onToast])

  useEffect(() => {
    load()
    const refresh = () => load()
    window.addEventListener('hugginghack:repository-changed', refresh)
    return () => window.removeEventListener('hugginghack:repository-changed', refresh)
  }, [load])

  function startResume(repository: OwnedRepository) {
    setResume(repository)
    setWizardKey((key) => key + 1)
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    wizard.current?.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' })
  }

  const unfinished = repositories.filter((item) => item.status === 'uploading')
  const published = repositories.filter((item) => item.status === 'ready')

  return (
    <div className="standard-page uploads-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">Your repositories</span>
          <h1>Upload a model</h1>
          <p>Name it, choose who can see it and where it is stored, then add the folder.</p>
        </div>
      </div>
      <div ref={wizard} className="upload-wizard-anchor">
        <UploadWizard
          key={wizardKey}
          user={user}
          namespaces={namespaces}
          repositories={repositories}
          resume={resume}
          initialNamespace={searchParams.get('namespace') || user.username}
          onCreated={load}
          onExitResume={() => {
            setResume(null)
            setWizardKey((key) => key + 1)
          }}
          onToast={onToast}
        />
      </div>

      {unfinished.length > 0 && (
        <section className="repository-section">
          <div className="section-heading">
            <div>
              <span className="eyebrow">Created, but not every file has arrived</span>
              <h2>Unfinished uploads</h2>
            </div>
            <span>{unfinished.length} {unfinished.length === 1 ? 'repository' : 'repositories'}</span>
          </div>
          <div className="repository-grid">
            {unfinished.map((repository) => (
              <RepositoryRow
                key={repository.id}
                repository={repository}
                onResume={() => startResume(repository)}
                onChanged={load}
                onToast={onToast}
              />
            ))}
          </div>
        </section>
      )}

      <section className="repository-section">
        <div className="section-heading">
          <div>
            <span className="eyebrow">Yours and your organizations’</span>
            <h2>Published models</h2>
          </div>
          <span>{loaded ? `${published.length} ${published.length === 1 ? 'model' : 'models'}` : ''}</span>
        </div>
        {!loaded ? (
          <RowSkeletons rows={3} cells={3} label="Loading your repositories" />
        ) : (
          <div className="repository-grid">
            {published.map((repository) => (
              <RepositoryRow
                key={repository.id}
                repository={repository}
                onResume={() => startResume(repository)}
                onChanged={load}
                onToast={onToast}
              />
            ))}
            {published.length === 0 && <div className="empty-compact">Models you upload appear here.</div>}
          </div>
        )}
      </section>
    </div>
  )
}
