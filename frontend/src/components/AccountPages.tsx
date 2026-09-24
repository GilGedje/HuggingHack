import {
  Archive,
  BookMarked,
  Check,
  Eye,
  KeyRound,
  EyeOff,
  FileUp,
  FolderHeart,
  HardDrive,
  Heart,
  LoaderCircle,
  LockKeyhole,
  Plus,
  ShieldCheck,
  Trash2,
  UploadCloud,
  Users,
  X,
} from 'lucide-react'
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from 'react'
import { api } from '../api'
import type {
  AuthStatus,
  Collection,
  Health,
  OwnedRepository,
  SavedModel,
  StorageOption,
  UploadNamespace,
  User,
} from '../types'
import { formatBytes, relativeTime, taskLabel } from '../utils'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { relativeUploadPath, useUploads } from '../uploads'
import { NamespacePicker } from './NamespacePicker'

type ToastHandler = (message: string, tone?: 'success' | 'error') => void

function takeSsoError(): string {
  // The sign-in callback reports problems as #/?sso_error=...; show it once.
  const [path, query = ''] = window.location.hash.replace(/^#/, '').split('?')
  const params = new URLSearchParams(query)
  const message = params.get('sso_error') || ''
  if (message) {
    params.delete('sso_error')
    const rest = params.toString()
    window.history.replaceState(null, '', `#${path || '/'}${rest ? `?${rest}` : ''}`)
  }
  return message
}

function currentPath(): string {
  const path = window.location.hash.replace(/^#/, '')
  return path.startsWith('/') && !path.startsWith('//') ? path : '/models'
}

export function AuthScreen({
  setup,
  oidc,
  onAuthenticated,
}: {
  setup: boolean
  oidc?: { enabled: boolean; name: string }
  onAuthenticated: (status: AuthStatus) => void
}) {
  const [username, setUsername] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(takeSsoError)
  const sso = !setup && oidc?.enabled

  async function submit(event: FormEvent) {
    event.preventDefault()
    setSubmitting(true)
    setError('')
    try {
      const status = setup
        ? await api.setup({ username, display_name: displayName, password })
        : await api.login({ username, password })
      onAuthenticated(status)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Unable to continue')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="auth-layout">
      <section className="auth-story">
        <img src="/hugginghack-mark.svg" alt="" />
        <span className="eyebrow">Your model library, with a front door</span>
        <h1>{setup ? 'Create the owner account' : 'Welcome back'}</h1>
        <p>
          {setup
            ? 'The first account administers this HuggingHack instance. Your models stay on this machine or NAS.'
            : 'Sign in to your saved models, collections, downloads, and uploaded repositories.'}
        </p>
        <div className="auth-benefits">
          <span><FolderHeart size={18} /> Personal collections</span>
          <span><UploadCloud size={18} /> Resumable uploads</span>
          <span><ShieldCheck size={18} /> Private by default</span>
        </div>
      </section>
      <form className="auth-card" onSubmit={submit}>
        <div>
          <span className="eyebrow">{setup ? 'One-time setup' : 'HuggingHack account'}</span>
          <h2>{setup ? 'Set up your library' : 'Sign in'}</h2>
        </div>
        {sso && (
          <>
            <a
              className="download-button auth-submit sso-button"
              href={`/api/auth/oidc/login?next=${encodeURIComponent(currentPath())}`}
            >
              <KeyRound size={17} /> Sign in with {oidc?.name || 'single sign-on'}
            </a>
            <div className="auth-divider"><span>or use a HuggingHack password</span></div>
          </>
        )}
        {setup && (
          <label>
            Display name
            <input
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
              placeholder="Your name"
              maxLength={80}
              autoComplete="name"
            />
          </label>
        )}
        <label>
          Username
          <input
            value={username}
            onChange={(event) => setUsername(event.target.value.toLowerCase())}
            placeholder="modelkeeper"
            maxLength={32}
            autoCapitalize="none"
            autoComplete="username"
            required
          />
          {setup && <small>3-32 lowercase letters, numbers, underscores, or hyphens.</small>}
        </label>
        <label>
          Password
          <span className="password-field">
            <input
              type={showPassword ? 'text' : 'password'}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              minLength={setup ? 12 : 1}
              maxLength={256}
              autoComplete={setup ? 'new-password' : 'current-password'}
              required
            />
            <button
              type="button"
              onClick={() => setShowPassword((value) => !value)}
              aria-label={showPassword ? 'Hide password' : 'Show password'}
            >
              {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
            </button>
          </span>
          {setup && <small>Use at least 12 characters. HuggingHack stores a salted scrypt hash.</small>}
        </label>
        {error && <div className="inline-error">{error}</div>}
        <button className={sso ? 'secondary-button auth-submit' : 'download-button auth-submit'} disabled={submitting}>
          {submitting ? <LoaderCircle size={17} className="spin" /> : <LockKeyhole size={17} />}
          {submitting ? 'Working…' : setup ? 'Create owner account' : 'Sign in'}
        </button>
      </form>
    </main>
  )
}

export function SavedPage({ onToast }: { onToast: ToastHandler }) {
  const [items, setItems] = useState<SavedModel[]>([])
  const [collections, setCollections] = useState<Collection[]>([])
  const [collectionId, setCollectionId] = useState('')
  const [query, setQuery] = useState('')
  const [newCollection, setNewCollection] = useState('')
  const [loading, setLoading] = useState(true)
  const navigate = useNavigate()
  const [editing, setEditing] = useState<string | null>(null)
  const [draftNote, setDraftNote] = useState('')
  const [draftCollections, setDraftCollections] = useState<string[]>([])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [saved, groups] = await Promise.all([
        api.savedModels(query, collectionId),
        api.collections(),
      ])
      setItems(saved.items)
      setCollections(groups.items)
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Unable to load saved models', 'error')
    } finally {
      setLoading(false)
    }
  }, [collectionId, onToast, query])

  useEffect(() => {
    const timer = window.setTimeout(load, 200)
    return () => window.clearTimeout(timer)
  }, [load])

  async function createCollection(event: FormEvent) {
    event.preventDefault()
    if (!newCollection.trim()) return
    try {
      await api.createCollection({ name: newCollection.trim() })
      setNewCollection('')
      await load()
      onToast('Collection created.')
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Unable to create collection', 'error')
    }
  }

  async function remove(item: SavedModel) {
    try {
      await api.unsaveModel(item.repo_id)
      await load()
      onToast(`${item.repo_id} was removed from your saved library.`)
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Unable to remove model', 'error')
    }
  }

  async function saveChanges(item: SavedModel) {
    try {
      await api.saveModel({
        repo_id: item.repo_id,
        note: draftNote,
        collection_ids: draftCollections,
        metadata: item.metadata,
      })
      setEditing(null)
      await load()
      onToast('Saved model updated.')
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Unable to update model', 'error')
    }
  }

  return (
    <>
      <div className="standard-page saved-page">
        <div className="page-heading">
          <div>
            <span className="eyebrow">Your shortlist across sessions</span>
            <h1>Saved models</h1>
            <p>Keep promising repositories close, add private notes, and organize them by project or rig.</p>
          </div>
        </div>
        <div className="library-workspace">
          <aside className="collection-sidebar">
            <button
              className={collectionId === '' ? 'active' : ''}
              onClick={() => setCollectionId('')}
            >
              <BookMarked size={16} /> All saved <span>{collectionId === '' ? items.length : ''}</span>
            </button>
            {collections.map((collection) => (
              <button
                key={collection.id}
                className={collectionId === collection.id ? 'active' : ''}
                onClick={() => setCollectionId(collection.id)}
              >
                <Archive size={15} />
                <span>{collection.name}</span>
                <em>{collection.model_count}</em>
              </button>
            ))}
            <form onSubmit={createCollection}>
              <input
                value={newCollection}
                onChange={(event) => setNewCollection(event.target.value)}
                placeholder="New collection"
                maxLength={80}
              />
              <button aria-label="Create collection"><Plus size={15} /></button>
            </form>
          </aside>
          <section className="saved-library">
            <div className="catalog-search">
              <Heart size={17} />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search saved models and notes"
              />
            </div>
            {loading ? (
              <div className="drawer-loading"><LoaderCircle className="spin" size={22} /> Loading your library…</div>
            ) : (
              <div className="saved-grid">
                {items.map((item) => (
                  <article className="saved-card" key={item.id}>
                    <button className="saved-card-open" onClick={() => navigate(`/models/${item.repo_id}`)}>
                      <span className="saved-card-mark">{item.repo_id.slice(0, 2).toUpperCase()}</span>
                      <span>
                        <small>{item.repo_id.split('/')[0]}</small>
                        <strong>{item.repo_id.split('/').slice(1).join('/')}</strong>
                      </span>
                    </button>
                    <div className="saved-card-tags">
                      {item.metadata.pipeline_tag && <span>{taskLabel(item.metadata.pipeline_tag)}</span>}
                      {item.metadata.library_name && <span>{item.metadata.library_name}</span>}
                      {item.metadata.local && <span>On NAS</span>}
                    </div>
                    {editing === item.id ? (
                      <div className="saved-editor">
                        <textarea
                          value={draftNote}
                          onChange={(event) => setDraftNote(event.target.value)}
                          placeholder="Why is this model worth keeping?"
                          maxLength={1000}
                        />
                        <div className="collection-picks">
                          {collections.map((collection) => (
                            <label key={collection.id}>
                              <input
                                type="checkbox"
                                checked={draftCollections.includes(collection.id)}
                                onChange={() =>
                                  setDraftCollections((current) =>
                                    current.includes(collection.id)
                                      ? current.filter((id) => id !== collection.id)
                                      : [...current, collection.id],
                                  )
                                }
                              />
                              {collection.name}
                            </label>
                          ))}
                        </div>
                        <div className="saved-card-actions">
                          <button className="download-button compact" onClick={() => saveChanges(item)}>
                            <Check size={14} /> Save
                          </button>
                          <button className="secondary-button compact" onClick={() => setEditing(null)}>
                            <X size={14} /> Cancel
                          </button>
                        </div>
                      </div>
                    ) : (
                      <>
                        <p>{item.note || 'No private note yet.'}</p>
                        <div className="saved-card-footer">
                          <small>Saved {relativeTime(item.created_at)}</small>
                          <div>
                            <button
                              onClick={() => {
                                setEditing(item.id)
                                setDraftNote(item.note)
                                setDraftCollections(item.collections)
                              }}
                            >
                              Organize
                            </button>
                            <button className="danger-text" onClick={() => remove(item)}>Remove</button>
                          </div>
                        </div>
                      </>
                    )}
                  </article>
                ))}
                {items.length === 0 && (
                  <div className="empty-state spacious">
                    <FolderHeart size={34} />
                    <h2>Nothing saved here yet</h2>
                    <p>Use the heart on any model card to build a shortlist without downloading it.</p>
                  </div>
                )}
              </div>
            )}
          </section>
        </div>
      </div>
    </>
  )
}

export function UploadsPage({
  user,
  onToast,
}: {
  user: User
  onToast: ToastHandler
}) {
  const folderInput = useRef<HTMLInputElement>(null)
  const [repositories, setRepositories] = useState<OwnedRepository[]>([])
  const [health, setHealth] = useState<Health | null>(null)
  const [activeRepo, setActiveRepo] = useState('')
  const [slug, setSlug] = useState('')
  const [description, setDescription] = useState('')
  const [visibility, setVisibility] = useState<'private' | 'shared'>('private')
  const [files, setFiles] = useState<File[]>([])
  const [message, setMessage] = useState('')
  const [creating, setCreating] = useState(false)
  const [storageOptions, setStorageOptions] = useState<StorageOption[]>([])
  const [namespaces, setNamespaces] = useState<UploadNamespace[]>([])
  const [searchParams] = useSearchParams()
  const [namespace, setNamespace] = useState(searchParams.get('namespace') || user.username)
  const [storageTarget, setStorageTarget] = useState('')
  const { jobs, enqueue } = useUploads()

  const load = useCallback(async () => {
    try {
      const [repos, runtime, targets, spaces] = await Promise.all([
        api.uploadRepositories(),
        api.health(),
        api.storageOptions(),
        api.uploadNamespaces(),
      ])
      setNamespaces(spaces.items)
      setRepositories(repos.items)
      setHealth(runtime)
      setStorageOptions(targets.items)
      const preferred = user.preferences?.default_storage_target
      setStorageTarget(
        (current) =>
          current
          || (preferred && targets.items.some((item) => item.id === preferred) ? preferred : targets.default),
      )
      setActiveRepo((current) => current || repos.items.find((item) => item.status === 'uploading')?.repo_id || '')
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Unable to load repositories', 'error')
    }
  }, [onToast, user.id])

  useEffect(() => {
    load()
    const refresh = () => load()
    window.addEventListener('hugginghack:repository-changed', refresh)
    return () => window.removeEventListener('hugginghack:repository-changed', refresh)
  }, [load])

  useEffect(() => {
    folderInput.current?.setAttribute('webkitdirectory', '')
    folderInput.current?.setAttribute('directory', '')
  }, [])

  const totalBytes = useMemo(() => files.reduce((sum, file) => sum + file.size, 0), [files])
  const active = repositories.find((item) => item.repo_id === activeRepo)
  const queued = jobs.some(
    (job) => job.repoId === activeRepo && ['queued', 'uploading', 'committing'].includes(job.status),
  )

  async function createRepository(event: FormEvent) {
    event.preventDefault()
    setCreating(true)
    try {
      const repository = await api.createUploadRepository({
        slug,
        description,
        visibility,
        storage_target: storageTarget || undefined,
        namespace,
      })
      setSlug('')
      setDescription('')
      setActiveRepo(repository.repo_id)
      await load()
      onToast(`${repository.repo_id} is ready for files.`)
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Unable to create repository', 'error')
    } finally {
      setCreating(false)
    }
  }

  function uploadFolder() {
    if (!active || active.owner_id !== user.id || files.length === 0) return
    enqueue({
      kind: 'new',
      repoId: active.repo_id,
      items: files.map((file) => ({ file, path: relativeUploadPath(file) })),
      message: message.trim() || `Upload ${files.length} file${files.length === 1 ? '' : 's'}`,
    })
    setFiles([])
    setMessage('')
    onToast(`Uploading to ${active.repo_id}. You can keep browsing; progress stays at the bottom of the screen.`)
  }

  async function toggleVisibility(repository: OwnedRepository) {
    try {
      await api.updateUploadRepository(repository.repo_id, {
        description: repository.description,
        visibility: repository.visibility === 'private' ? 'shared' : 'private',
      })
      await load()
      onToast(`${repository.repo_id} visibility updated.`)
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Unable to update repository', 'error')
    }
  }

  async function deleteRepository(repository: OwnedRepository) {
    const confirmation = window.prompt(
      `This permanently deletes the repository files from model storage.\n\nType ${repository.repo_id} to continue:`,
    )
    if (confirmation !== repository.repo_id) return
    try {
      await api.deleteUploadRepository(repository.repo_id, confirmation)
      if (activeRepo === repository.repo_id) setActiveRepo('')
      await load()
      onToast(`${repository.repo_id} and its files were deleted.`)
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Unable to delete repository', 'error')
    }
  }

  return (
    <div className="standard-page uploads-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">Repositories you own</span>
          <h1>Upload models</h1>
          <p>Create a private or shared repository, then stream a model folder straight into NAS storage.</p>
        </div>
      </div>
      <section className="upload-hero">
        <div>
          <FileUp size={25} />
          <strong>Large-file friendly</strong>
          <p>Files move in resumable {formatBytes(health?.upload_chunk_bytes || 8 * 1024 * 1024)} chunks and never enter the metadata database.</p>
        </div>
        <div>
          <HardDrive size={25} />
          <strong>{health ? formatBytes(health.storage.free_bytes) : 'Reading…'} free</strong>
          <p>Uploads land in the same plain owner/repository layout as Hub downloads.</p>
        </div>
      </section>
      <div className="upload-columns">
        <section className="settings-section create-repository">
          <div className="settings-section-title">
            <Plus size={20} />
            <div><h2>New repository</h2><p>Private is the safest default.</p></div>
          </div>
          <form onSubmit={createRepository}>
            <div className="repo-name-grid">
              <label htmlFor="new-repo-owner">Owner</label>
              <span aria-hidden="true" />
              <label htmlFor="new-repo-name">Model name</label>
              <NamespacePicker
                id="new-repo-owner"
                namespaces={
                  namespaces.length
                    ? namespaces
                    : [{ name: user.username, kind: 'user', display_name: user.display_name }]
                }
                value={namespace}
                onChange={setNamespace}
              />
              <span className="repo-name-slash" aria-hidden="true">/</span>
              <input
                id="new-repo-name"
                value={slug}
                onChange={(event) => setSlug(event.target.value)}
                placeholder="GLM-5.3-NVFP4"
                autoComplete="off"
                spellCheck={false}
                required
              />
            </div>
            <label>
              Description
              <textarea value={description} onChange={(event) => setDescription(event.target.value)} maxLength={500} />
            </label>
            <label>
              Visibility
              <select value={visibility} onChange={(event) => setVisibility(event.target.value as 'private' | 'shared')}>
                <option value="private">
                  {namespace.toLowerCase() === user.username.toLowerCase() ? 'Private — only me' : `Private — ${namespace} members`}
                </option>
                <option value="shared">Shared — all local accounts</option>
              </select>
            </label>
            {storageOptions.length > 1 && (
              <label>
                Storage
                <select value={storageTarget} onChange={(event) => setStorageTarget(event.target.value)}>
                  {storageOptions.map((option) => (
                    <option key={option.id} value={option.id}>
                      {option.name}{option.kind === 's3' ? ' · S3' : ''}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <button className="download-button" disabled={creating}>
              {creating ? <LoaderCircle size={16} className="spin" /> : <Plus size={16} />}
              Create repository
            </button>
          </form>
        </section>
        <section className="settings-section upload-drop">
          <div className="settings-section-title">
            <UploadCloud size={20} />
            <div><h2>Add a model folder</h2><p>Select an unfinished repository and a folder.</p></div>
          </div>
          <label>
            Repository
            <select value={activeRepo} onChange={(event) => setActiveRepo(event.target.value)}>
              <option value="">Choose a repository</option>
              {repositories
                .filter((item) => item.status === 'uploading')
                .map((item) => <option key={item.id} value={item.repo_id}>{item.repo_id}</option>)}
            </select>
          </label>
          <label className="folder-picker">
            <input
              ref={folderInput}
              type="file"
              multiple
              onChange={(event) => setFiles(Array.from(event.target.files || []))}
            />
            <FileUp size={25} />
            <strong>{files.length ? `${files.length} files selected` : 'Choose model folder'}</strong>
            <span>{files.length ? formatBytes(totalBytes) : 'Config, tokenizer, weights, and documentation'}</span>
          </label>
          <label>
            Commit message
            <input
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              maxLength={200}
              placeholder={files.length ? `Upload ${files.length} file${files.length === 1 ? '' : 's'}` : 'Initial upload'}
            />
          </label>
          <button
            className="download-button"
            disabled={!active || !files.length || queued}
            onClick={uploadFolder}
          >
            {queued ? <LoaderCircle size={16} className="spin" /> : <UploadCloud size={16} />}
            {queued ? 'Uploading in the background…' : 'Upload and commit'}
          </button>
        </section>
      </div>
      <section className="repository-section">
        <div className="section-heading">
          <div><span className="eyebrow">Yours and your organizations'</span><h2>Repositories you can upload to</h2></div>
          <span>{repositories.length} total</span>
        </div>
        <div className="repository-grid">
          {repositories.map((repository) => (
            <article className="owned-repository" key={repository.id}>
              <div className="repository-icon">
                {repository.visibility === 'private' ? <LockKeyhole size={18} /> : <Users size={18} />}
              </div>
              <div>
                <small>{repository.owner_display_name}</small>
                <h3>
                  {repository.status === 'ready' ? (
                    <Link to={`/models/${repository.repo_id}`}>{repository.repo_id}</Link>
                  ) : (
                    repository.repo_id
                  )}
                </h3>
                <p>{repository.description || 'No description yet.'}</p>
                <div className="repo-stats">
                  <span className={`status-pill ${repository.status === 'ready' ? 'ok' : ''}`}>{repository.status}</span>
                  {repository.size_bytes != null && <span>{formatBytes(repository.size_bytes)}</span>}
                  {repository.file_count != null && <span>{repository.file_count} files</span>}
                </div>
              </div>
              <div className="repository-actions">
                {repository.status === 'ready' && (
                  <Link to={`/models/${repository.repo_id}?upload=1`}>
                    <UploadCloud size={14} /> Upload changes
                  </Link>
                )}
                {repository.my_role === 'admin' && (
                  <>
                    <button onClick={() => toggleVisibility(repository)}>
                      {repository.visibility === 'private' ? <Users size={14} /> : <LockKeyhole size={14} />}
                      {repository.visibility === 'private' ? 'Share locally' : 'Make private'}
                    </button>
                    <button className="danger-text" onClick={() => deleteRepository(repository)}>
                      <Trash2 size={14} /> Delete
                    </button>
                  </>
                )}
              </div>
            </article>
          ))}
          {repositories.length === 0 && <div className="empty-compact">No repositories you can upload to yet.</div>}
        </div>
      </section>
    </div>
  )
}
