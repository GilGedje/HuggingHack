import {
  AlertCircle,
  Archive,
  BookMarked,
  Check,
  Clock,
  Eye,
  KeyRound,
  EyeOff,
  FolderHeart,
  Heart,
  LoaderCircle,
  LockKeyhole,
  Plus,
  ShieldCheck,
  Trash2,
  UploadCloud,
  X,
} from 'lucide-react'
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from 'react'
import { api } from '../api'
import type {
  AuthStatus,
  Collection,
  SavedModel,
} from '../types'
import { relativeTime, taskLabel } from '../utils'
import { useNavigate } from 'react-router-dom'
import { RowSkeletons } from './Skeletons'
import { useConfirm } from './ConfirmDialog'
import { prefersReducedMotion, useFadeOnChange, useSlidingHighlight } from '../motion'
import { ssoErrorMessage } from '../ssoError'
import { focusAfterRemoval } from '../focus'
import { LoadError } from './LoadError'
import { brandMark, type Theme } from '../theme'

type ToastHandler = (message: string, tone?: 'success' | 'error') => void

function takeSsoError(): string {
  // The sign-in callback reports problems as #/?sso_error=...; show it once.
  const [path, query = ''] = window.location.hash.replace(/^#/, '').split('?')
  const params = new URLSearchParams(query)
  const code = params.get('sso_error') || ''
  if (code) {
    params.delete('sso_error')
    const rest = params.toString()
    window.history.replaceState(null, '', `#${path || '/'}${rest ? `?${rest}` : ''}`)
  }
  // Anyone can put text in a link, so only a fixed message is ever shown.
  return ssoErrorMessage(code)
}

function currentPath(): string {
  const path = window.location.hash.replace(/^#/, '')
  return path.startsWith('/') && !path.startsWith('//') ? path : '/models'
}

export function AuthScreen({
  theme,
  setup,
  oidc,
  notice,
  onAuthenticated,
}: {
  theme: Theme
  setup: boolean
  oidc?: { enabled: boolean; name: string }
  /** Why they are here, such as a session that expired. */
  notice?: string
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
      setError(reason instanceof Error ? reason.message : setup ? 'Could not create the account.' : 'Could not sign in.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="auth-layout">
      <section className="auth-story">
        <img src={brandMark(theme)} alt="" />
        <span className="eyebrow">Your model library, with a front door</span>
        <h1>{setup ? 'Create the owner account' : 'Welcome back'}</h1>
        <p>
          {setup
            ? 'The first account administers this HuggingHack instance. Your models stay on this machine or NAS.'
            : 'Sign in to your saved models, collections, and uploaded repositories.'}
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
        {/* Problems sit at the top, above every way in, where they are read first. */}
        {error ? (
          <div className="inline-error auth-message" role="alert">
            <AlertCircle size={16} /> {error}
          </div>
        ) : notice ? (
          <div className="auth-message auth-notice" role="status">
            <Clock size={16} /> {notice}
          </div>
        ) : null}
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
  // The collection whose models are on screen. It changes only once they have
  // arrived, so the grid fades from one collection's models straight to the next.
  const [shown, setShown] = useState<string | null>(null)
  const [removing, setRemoving] = useState<string | null>(null)
  const [error, setError] = useState('')
  const latest = useRef(0)
  const grid = useFadeOnChange<HTMLDivElement>(shown ?? '')
  const sidebar = useSlidingHighlight<HTMLElement>(`${collectionId}:${collections.map((item) => item.id).join(',')}`)
  const confirm = useConfirm()
  const navigate = useNavigate()
  const [editing, setEditing] = useState<string | null>(null)
  // Closing the editor brings back that model's Organize button; focus returns to it.
  const refocusOrganize = useRef<string | null>(null)
  function closeEditor(id: string) {
    refocusOrganize.current = id
    setEditing(null)
  }
  const [draftNote, setDraftNote] = useState('')
  const [draftCollections, setDraftCollections] = useState<string[]>([])

  const load = useCallback(async () => {
    const request = ++latest.current
    setLoading(true)
    try {
      const [saved, groups] = await Promise.all([
        api.savedModels(query, collectionId),
        api.collections(),
      ])
      // A quicker click may have asked for another collection meanwhile.
      if (request !== latest.current) return
      setItems(saved.items)
      setCollections(groups.items)
      setShown(collectionId)
      setError('')
    } catch (reason) {
      if (request === latest.current) setError(reason instanceof Error ? reason.message : 'The server did not answer.')
    } finally {
      if (request === latest.current) setLoading(false)
    }
  }, [collectionId, query])

  // Typing waits a moment before searching; picking a collection loads at once.
  const lastQuery = useRef(query)
  useEffect(() => {
    const typed = lastQuery.current !== query
    lastQuery.current = query
    const timer = window.setTimeout(load, typed ? 200 : 0)
    return () => window.clearTimeout(timer)
  }, [load, query])

  async function createCollection(event: FormEvent) {
    event.preventDefault()
    if (!newCollection.trim()) return
    try {
      await api.createCollection({ name: newCollection.trim() })
      setNewCollection('')
      await load()
      onToast('Collection created.')
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Could not create the collection.', 'error')
    }
  }

  async function deleteCollection(collection: Collection, trigger: HTMLElement) {
    const row = trigger.parentElement
    const refocus = focusAfterRemoval(trigger, row)
    const count = collection.model_count
    const sure = await confirm({
      title: `Delete “${collection.name}”?`,
      message: count
        ? `The ${count === 1 ? 'model' : `${count} models`} in it stay${count === 1 ? 's' : ''} saved under All saved; only the collection goes.`
        : 'The collection is empty. Nothing else changes.',
      confirmLabel: 'Delete collection',
      danger: true,
    })
    if (!sure) return
    setRemoving(collection.id)
    try {
      await api.deleteCollection(collection.id)
      // Fold the row away before it leaves the list, so the rows below slide up.
      if (row?.animate && !prefersReducedMotion()) {
        await row
          .animate(
            [
              { height: `${row.offsetHeight}px`, minHeight: '0px', opacity: 1 },
              { height: '0px', minHeight: '0px', opacity: 0 },
            ],
            { duration: 200, easing: 'cubic-bezier(0.32, 0.72, 0, 1)', fill: 'forwards' },
          )
          .finished.catch(() => undefined)
      }
      setCollections((current) => current.filter((item) => item.id !== collection.id))
      if (collectionId === collection.id) setCollectionId('')
      else await load()
      onToast(`Collection “${collection.name}” deleted.`)
      refocus()
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Could not delete the collection.', 'error')
    } finally {
      setRemoving(null)
    }
  }

  async function remove(item: SavedModel, trigger: HTMLElement) {
    const refocus = focusAfterRemoval(trigger, trigger.closest('article'))
    try {
      await api.unsaveModel(item.repo_id)
      await load()
      onToast(`${item.repo_id} was removed from your saved library.`)
      refocus()
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Could not remove the model.', 'error')
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
      closeEditor(item.id)
      await load()
      onToast('Saved model updated.')
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Could not save your changes.', 'error')
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
          <aside className="collection-sidebar" ref={sidebar}>
            <button
              className={collectionId === '' ? 'active' : ''}
              onClick={() => setCollectionId('')}
            >
              <BookMarked size={16} /> All saved <span>{collectionId === '' ? items.length : ''}</span>
            </button>
            {collections.map((collection) => (
              <div
                key={collection.id}
                className={collectionId === collection.id ? 'collection-row active' : 'collection-row'}
                aria-busy={removing === collection.id || undefined}
              >
                <button className="collection-open" onClick={() => setCollectionId(collection.id)}>
                  <Archive size={15} />
                  <span>{collection.name}</span>
                  <em>{collection.model_count}</em>
                </button>
                <button
                  className="collection-delete"
                  aria-label={`Delete collection ${collection.name}`}
                  title="Delete collection"
                  disabled={removing !== null}
                  onClick={(event) => deleteCollection(collection, event.currentTarget)}
                >
                  <Trash2 size={13} />
                </button>
              </div>
            ))}
            <form onSubmit={createCollection}>
              <input
                value={newCollection}
                onChange={(event) => setNewCollection(event.target.value)}
                placeholder="New collection"
                aria-label="New collection name"
                maxLength={80}
              />
              <button aria-label="Create collection" disabled={!newCollection.trim()}><Plus size={15} /></button>
            </form>
          </aside>
          <section className="saved-library">
            <div className="catalog-search">
              <Heart size={17} />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search saved models and notes"
                aria-label="Search saved models and notes"
              />
            </div>
            {error ? (
              <LoadError what="your saved models" message={error} onRetry={load} />
            ) : loading && shown === null ? (
              <RowSkeletons rows={4} cells={0} label="Loading your saved models" />
            ) : (
              <div ref={grid} className={loading ? 'saved-grid refreshing' : 'saved-grid'} aria-busy={loading || undefined}>
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
                          // Organize opens this in place of the button, so focus follows it here.
                          autoFocus
                          aria-label={`Private note for ${item.repo_id}`}
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
                          <button className="secondary-button compact" onClick={() => closeEditor(item.id)}>
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
                              ref={(node) => {
                                if (node && refocusOrganize.current === item.id) {
                                  refocusOrganize.current = null
                                  node.focus()
                                }
                              }}
                              onClick={() => {
                                setEditing(item.id)
                                setDraftNote(item.note)
                                setDraftCollections(item.collections)
                              }}
                            >
                              Organize
                            </button>
                            <button className="danger-text" onClick={(event) => remove(item, event.currentTarget)}>Remove</button>
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
