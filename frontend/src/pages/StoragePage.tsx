import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertCircle,
  AlertTriangle,
  ArrowRightLeft,
  Building2,
  Check,
  Cloud,
  Database,
  FolderCog,
  HardDrive,
  LoaderCircle,
  LockKeyhole,
  RefreshCw,
  Search,
  UserRound,
  Users,
  X,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { useAccess } from '../access'
import { api } from '../api'
import { formatLabels } from '../components/RepositoryRows'
import { MoveModelDialog } from '../components/MoveModelDialog'
import { useFadeOnChange } from '../motion'
import { MOVE_STEPS, moveCancellable, movePercent, moveStep, moveUnfinished } from '../storageMoves'
import type { StorageGrant, StorageModel, StorageMove, StorageOverview, StorageTarget } from '../types'
import { formatBytes, formatNumber, relativeTime } from '../utils'
import { visibilityLabel } from '../visibility'
import { RowSkeletons, StorageSkeleton } from '../components/Skeletons'
import { LoadError } from '../components/LoadError'

type ToastHandler = (message: string, tone?: 'success' | 'error') => void

function location(target: StorageTarget): string {
  if (target.kind === 'filesystem') return target.path || '/models'
  return `s3://${target.bucket}${target.prefix ? `/${target.prefix}` : ''}`
}

function Capacity({ used, total }: { used: number; total: number }) {
  const percent = total ? Math.min(100, (used / total) * 100) : 0
  return (
    <div className="capacity-track" aria-label={`${percent.toFixed(0)} percent used`}>
      <span style={{ width: `${percent}%` }} />
    </div>
  )
}

type Principal = Pick<StorageGrant, 'kind' | 'name' | 'display_name'>

const same = (a: Principal, b: Principal) => a.kind === b.kind && a.name.toLowerCase() === b.name.toLowerCase()

/** Who may put new repositories in a storage location. Nobody listed means everyone. */
function UploadAccess({
  target,
  canManage,
  onSaved,
  onToast,
}: {
  target: StorageTarget
  canManage: boolean
  onSaved: () => void
  onToast: ToastHandler
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<Principal[]>(target.grants)
  const [search, setSearch] = useState('')
  const [results, setResults] = useState<Principal[]>([])
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (!editing || !search.trim()) {
      setResults([])
      return
    }
    let ignore = false
    const timer = window.setTimeout(() => {
      Promise.all([
        api.adminUsers({ q: search, role: '', status: '', sort: 'name', page: 1, per_page: 6 }),
        api.adminOrganizations({ q: search, filter: '', sort: 'name', page: 1, per_page: 6 }),
      ])
        .then(([users, organizations]) => {
          if (ignore) return
          setResults([
            ...organizations.items.map((item) => ({ kind: 'organization' as const, name: item.name, display_name: item.display_name })),
            ...users.items.map((item) => ({ kind: 'user' as const, name: item.username, display_name: item.display_name })),
          ])
        })
        .catch(() => undefined)
    }, 180)
    return () => {
      ignore = true
      window.clearTimeout(timer)
    }
  }, [editing, search])

  async function save() {
    setSaving(true)
    try {
      await api.updateStorageGrants(target.id, {
        users: draft.filter((item) => item.kind === 'user').map((item) => item.name),
        organizations: draft.filter((item) => item.kind === 'organization').map((item) => item.name),
      })
      setEditing(false)
      onSaved()
      onToast(draft.length ? `${target.name} is now reserved.` : `${target.name} is open to every uploader.`)
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Could not save upload access.', 'error')
    } finally {
      setSaving(false)
    }
  }

  const Chip = ({ item, onRemove }: { item: Principal; onRemove?: () => void }) => (
    <span className="access-chip">
      {item.kind === 'organization' ? <Building2 size={12} /> : <UserRound size={12} />}
      {item.name}
      {onRemove && (
        <button type="button" aria-label={`Remove ${item.name}`} onClick={onRemove}>
          <X size={12} />
        </button>
      )}
    </span>
  )

  if (!editing) {
    return (
      <div className="upload-access">
        <span className="upload-access-label">Who can upload</span>
        {target.grants.length ? (
          target.grants.map((item) => <Chip key={`${item.kind}-${item.id}`} item={item} />)
        ) : (
          <span className="upload-access-open">Everyone who can upload</span>
        )}
        {canManage && (
          <button
            type="button"
            className="secondary-button compact"
            onClick={() => {
              setDraft(target.grants)
              setSearch('')
              setEditing(true)
            }}
          >
            {target.grants.length ? 'Edit' : 'Reserve'}
          </button>
        )}
      </div>
    )
  }

  const available = results.filter((item) => !draft.some((chosen) => same(chosen, item)))
  return (
    <div className="upload-access editing">
      <span className="upload-access-label">Who can upload</span>
      <p>
        Only the users and organizations listed here can create repositories in {target.name}: users for
        their personal repositories, organizations for theirs. Leave it empty to open it to everyone.
      </p>
      <div className="upload-access-chips">
        {draft.map((item) => (
          <Chip
            key={`${item.kind}-${item.name}`}
            item={item}
            onRemove={() => setDraft(draft.filter((chosen) => !same(chosen, item)))}
          />
        ))}
        {!draft.length && <span className="upload-access-open">Everyone who can upload</span>}
      </div>
      <div className="catalog-search upload-access-search">
        <Search size={16} />
        <input
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Add a user or organization"
          aria-label="Find a user or organization"
          autoFocus
        />
      </div>
      {available.length > 0 && (
        <ul className="upload-access-results">
          {available.map((item) => (
            <li key={`${item.kind}-${item.name}`}>
              <button
                type="button"
                onClick={() => {
                  setDraft([...draft, item])
                  setSearch('')
                }}
              >
                {item.kind === 'organization' ? <Building2 size={13} /> : <UserRound size={13} />}
                <strong>{item.name}</strong>
                <small>{item.kind === 'organization' ? `Organization · ${item.display_name}` : item.display_name}</small>
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="upload-access-actions">
        <button type="button" className="download-button compact" onClick={save} disabled={saving}>
          {saving ? <LoaderCircle size={14} className="spin" /> : <Check size={14} />} Save
        </button>
        <button type="button" className="secondary-button compact" onClick={() => setEditing(false)} disabled={saving}>
          Cancel
        </button>
      </div>
    </div>
  )
}

/** A move under way, folded open under its model's row: the four steps, overall
 * progress, and what it is doing now. */
function MoveProgress({
  move,
  destination,
  onCancel,
}: {
  move: StorageMove
  destination: string
  onCancel: (move: StorageMove) => void
}) {
  const step = moveStep(move)
  const message = useFadeOnChange<HTMLSpanElement>(move.status)
  const bytes =
    move.status === 'copying'
      ? `${formatBytes(move.copied_bytes)} of ${formatBytes(move.total_bytes)}`
      : move.status === 'verifying'
        ? `${formatBytes(move.verified_bytes)} of ${formatBytes(move.total_bytes)} checked`
        : move.status === 'draining' && move.active_reads
          ? `${move.active_reads} download${move.active_reads === 1 ? '' : 's'} still reading the old copy`
          : ''
  return (
    <div className="move-progress" role="status" aria-label={`Moving ${move.repo_id} to ${destination}`}>
      <div>
        <div className="move-progress-head">
          <ArrowRightLeft size={13} />
          <strong>Moving to {destination}</strong>
          <ol className="move-steps" aria-label="Steps">
            {MOVE_STEPS.map((label, index) => (
              <li key={label} className={index < step ? 'complete' : index === step ? 'current' : undefined}>
                <span className="move-step-dot">{index < step && <Check size={9} />}</span>
                {label}
              </li>
            ))}
          </ol>
        </div>
        <div className="job-progress live">
          <span style={{ width: `${movePercent(move)}%` }} />
        </div>
        <div className="move-progress-meta">
          <span ref={message}>{move.message}</span>
          <span>{bytes}</span>
          {moveCancellable(move) && (
            <button type="button" className="text-link" onClick={() => onCancel(move)}>Cancel</button>
          )}
        </div>
      </div>
    </div>
  )
}

function TargetSection({
  target,
  query,
  scanning,
  canManage,
  canMove,
  moves,
  targetNames,
  onMove,
  onCancelMove,
  onChanged,
  onToast,
}: {
  target: StorageTarget
  /** Another location exists to move to. */
  canMove: boolean
  /** The move running for each repository, if any. */
  moves: Map<string, StorageMove>
  targetNames: Record<string, string>
  onMove: (model: StorageModel) => void
  onCancelMove: (move: StorageMove) => void
  query: string
  /** A scan is re-reading every location, so the table shows what is coming. */
  scanning: boolean
  canManage: boolean
  onChanged: () => void
  onToast: ToastHandler
}) {
  const models = target.models.filter((model) =>
    model.repo_id.toLowerCase().includes(query.trim().toLowerCase()),
  )
  const Icon = target.kind === 's3' ? Cloud : HardDrive
  return (
    <section className="storage-target" aria-label={target.name}>
      <header className="storage-target-header">
        <div className="storage-icon">
          <Icon size={22} />
        </div>
        <div className="storage-target-title">
          <div>
            <h2>{target.name}</h2>
            {target.default && <span className="local-badge">Default for new models</span>}
          </div>
          <code>{location(target)}</code>
          {target.endpoint && <small>{target.endpoint}{target.region ? ` · ${target.region}` : ''}</small>}
        </div>
        <span className={target.connected ? 'status-pill ok' : 'status-pill danger'}>
          {target.connected ? <Check size={13} /> : <AlertCircle size={13} />}
          {target.connected ? 'Connected' : 'Offline'}
        </span>
      </header>
      {target.error && (
        <div className="inline-error storage-target-error">
          <AlertTriangle size={16} /> {target.error}
        </div>
      )}
      <div className="storage-target-stats">
        <span>
          <strong>{formatNumber(target.model_count)}</strong> model{target.model_count === 1 ? '' : 's'}
        </span>
        <span>
          <strong>{formatBytes(target.total_bytes)}</strong> of models
        </span>
        {target.kind === 's3' ? (
          <span>
            <strong>{formatNumber(target.cached_count)}</strong> cached locally
          </span>
        ) : (
          <span>
            <strong>{target.capacity ? formatBytes(target.capacity.free_bytes) : '—'}</strong> free
          </span>
        )}
      </div>
      {target.capacity && (
        <Capacity used={target.capacity.used_bytes} total={target.capacity.total_bytes} />
      )}
      <UploadAccess target={target} canManage={canManage} onSaved={onChanged} onToast={onToast} />
      {scanning ? (
        <RowSkeletons rows={Math.min(Math.max(target.models.length, 2), 5)} cells={4} label={`Scanning ${target.name}`} />
      ) : (
        <div className="storage-model-table" role="table" aria-label={`Models in ${target.name}`}>
          <div className="storage-model-row header" role="row">
            <span role="columnheader">Model</span>
            <span role="columnheader">Size</span>
            <span role="columnheader">Files</span>
            <span role="columnheader">Parameters</span>
            <span role="columnheader">Status</span>
            <span role="columnheader">Updated</span>
            <span role="columnheader"><span className="sr-only">Actions</span></span>
          </div>
          {models.map((model) => {
            const move = moves.get(model.repo_id)
            return (
              <div className={move ? 'storage-model-item moving' : 'storage-model-item'} role="rowgroup" key={model.repo_id}>
                <div className="storage-model-row" role="row">
                  <span role="cell" className="storage-model-name">
                    <Link to={`/models/${model.repo_id}`}>{model.repo_id}</Link>
                    <small>
                      {model.formats.map((format) => formatLabels[format] || format).join(' · ') || 'No weights'}
                      {model.visibility !== 'public' && (
                        <>
                          {' · '}
                          {model.visibility === 'private' ? <LockKeyhole size={10} /> : <Users size={10} />}
                          {visibilityLabel(model.visibility)}
                        </>
                      )}
                    </small>
                  </span>
                  <span role="cell" data-label="Size">{formatBytes(model.size_bytes)}</span>
                  <span role="cell" data-label="Files">{formatNumber(model.file_count)}</span>
                  <span role="cell" data-label="Parameters">{model.parameter_count ? formatNumber(model.parameter_count) : '—'}</span>
                  <span role="cell" data-label="Status">
                    {target.kind === 'filesystem' ? 'On disk' : model.cached ? 'Cached' : 'S3 only'}
                  </span>
                  <span role="cell" data-label="Updated">{relativeTime(model.modified_at)}</span>
                  <span role="cell" className="storage-model-actions">
                    {canManage && canMove && !move && (
                      <button type="button" className="secondary-button compact" onClick={() => onMove(model)}>
                        <ArrowRightLeft size={13} /> Move
                      </button>
                    )}
                  </span>
                </div>
                {move && (
                  <MoveProgress
                    move={move}
                    destination={targetNames[move.destination_target] || move.destination_target}
                    onCancel={onCancelMove}
                  />
                )}
              </div>
            )
          })}
          {models.length === 0 && (
            <div className="empty-compact">
              {target.models.length ? 'No models match your search.' : 'No models in this location yet.'}
            </div>
          )}
        </div>
      )}
    </section>
  )
}

export function StoragePage({ onToast }: { onToast: ToastHandler }) {
  const { can } = useAccess()
  const [overview, setOverview] = useState<StorageOverview | null>(null)
  const [error, setError] = useState('')
  const [scanning, setScanning] = useState(false)
  const [query, setQuery] = useState('')
  const [moves, setMoves] = useState<StorageMove[]>([])
  const [moving, setMoving] = useState<{ model: StorageModel; source: StorageTarget } | null>(null)
  const known = useRef<Map<string, string>>(new Map())

  const load = useCallback(() => {
    setError('')
    api
      .storageTargets()
      .then(setOverview)
      .catch((reason) => setError(reason instanceof Error ? reason.message : 'The server did not answer.'))
  }, [])

  const loadMoves = useCallback(() => {
    api
      .storageMoves()
      .then((payload) => {
        // A move that just finished changes which location lists the model.
        let finished = false
        for (const move of payload.items) {
          const before = known.current.get(move.id)
          if (before && moveUnfinished({ status: before }) && !moveUnfinished(move)) {
            finished = true
            if (move.status === 'done') onToast(`${move.repo_id} moved.`)
            else if (move.status === 'failed') onToast(`${move.repo_id}: ${move.error || 'the move failed'}`, 'error')
            else onToast(`The move of ${move.repo_id} was cancelled.`)
          }
          known.current.set(move.id, move.status)
        }
        setMoves(payload.items)
        if (finished) load()
      })
      .catch(() => undefined)
  }, [load, onToast])

  const running = moves.filter(moveUnfinished)
  const activeMoves = useMemo(
    () => new Map(moves.filter(moveUnfinished).map((move) => [move.repo_id, move])),
    [moves],
  )

  useEffect(() => {
    loadMoves()
  }, [loadMoves])

  // Poll only while something is moving.
  useEffect(() => {
    if (!running.length) return
    const timer = window.setInterval(loadMoves, 1500)
    return () => window.clearInterval(timer)
  }, [running.length, loadMoves])

  async function cancelMove(move: StorageMove) {
    try {
      await api.cancelStorageMove(move.id)
      loadMoves()
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Could not cancel the move.', 'error')
    }
  }

  useEffect(() => {
    load()
    window.addEventListener('hugginghack:repository-changed', load)
    return () => window.removeEventListener('hugginghack:repository-changed', load)
  }, [load])

  async function scan() {
    setScanning(true)
    try {
      const result = await api.scanLocalModels()
      onToast(`Storage scan complete: ${result.count} model${result.count === 1 ? '' : 's'} indexed.`)
      load()
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Storage scan failed', 'error')
    } finally {
      setScanning(false)
    }
  }

  const totals = useMemo(() => {
    const targets = overview?.targets || []
    return {
      models: targets.reduce((sum, target) => sum + target.model_count, 0),
      bytes: targets.reduce((sum, target) => sum + target.total_bytes, 0),
    }
  }, [overview])

  return (
    <div className="standard-page storage-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">Administration</span>
          <h1>Storage</h1>
          <p>Every bucket and folder that holds models, with the models stored in each.</p>
        </div>
        <button className="secondary-button" onClick={scan} disabled={scanning}>
          {scanning ? <LoaderCircle size={16} className="spin" /> : <RefreshCw size={16} />}
          {scanning ? 'Scanning…' : 'Scan storage'}
        </button>
      </div>

      {error && <LoadError what="storage locations" message={error} onRetry={load} />}
      {!overview && !error && <StorageSkeleton />}

      {overview && (
        <>
          <section className="storage-strip">
            <div className="storage-icon">
              <Database size={23} />
            </div>
            <div className="storage-main">
              <div className="storage-title">
                <strong>
                  {overview.targets.length} location{overview.targets.length === 1 ? '' : 's'} ·{' '}
                  {formatNumber(totals.models)} model{totals.models === 1 ? '' : 's'} · {formatBytes(totals.bytes)}
                </strong>
                <code>Working cache {overview.cache.path}</code>
              </div>
              <Capacity used={overview.cache.used_bytes} total={overview.cache.total_bytes} />
              <div className="storage-meta">
                <span>{formatBytes(overview.cache.free_bytes)} free in the working cache</span>
                <span>
                  {formatNumber(overview.cache.model_count)} model{overview.cache.model_count === 1 ? '' : 's'} on disk ·{' '}
                  {formatBytes(overview.cache.model_bytes)}
                </span>
              </div>
            </div>
          </section>

          <section className={overview.system.remote && overview.system.ok ? 'system-strip' : 'system-strip warning'} aria-label="Site data">
            <FolderCog size={18} />
            <div>
              <strong>Site data · {overview.system.name}</strong>
              <code>{overview.system.location}</code>
              <small>
                {!overview.system.ok
                  ? `Not reachable right now: ${overview.system.error || 'unknown error'}. Pictures show initials until it is back.`
                  : overview.system.remote
                    ? 'Profile pictures and git history live here, in a hidden _system folder, so the server keeps no data of its own besides the database and its caches.'
                    : 'Profile pictures and git history are on this server’s disk. Set SYSTEM_STORAGE_TARGET to an S3 location to keep them in S3 with the models.'}
              </small>
            </div>
            <span className={overview.system.ok ? 'status-pill ok' : 'status-pill danger'}>
              {overview.system.ok ? <Check size={13} /> : <AlertCircle size={13} />}
              {overview.system.ok ? (overview.system.remote ? 'In S3' : 'Local disk') : 'Offline'}
            </span>
          </section>

          {overview.conflicts.length > 0 && (
            <div className="security-note warning storage-conflicts">
              <AlertTriangle size={16} />
              <div>
                <strong>
                  {overview.conflicts.length} model{overview.conflicts.length === 1 ? ' exists' : 's exist'} in
                  more than one location
                </strong>
                <ul>
                  {overview.conflicts.map((conflict) => (
                    <li key={`${conflict.repo_id}-${conflict.skipped_target}`}>
                      <code>{conflict.repo_id}</code> is served from <code>{conflict.kept_target}</code>; the
                      copy in <code>{conflict.skipped_target}</code> is ignored.
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          )}

          <div className="local-tools">
            <div className="catalog-search">
              <Search size={18} />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Filter models in every location"
              />
            </div>
          </div>

          {overview.targets.map((target) => (
            <TargetSection
              key={target.id}
              target={target}
              query={query}
              scanning={scanning}
              canManage={can('storage.manage')}
              canMove={overview.targets.length > 1}
              moves={activeMoves}
              targetNames={Object.fromEntries(overview.targets.map((item) => [item.id, item.name]))}
              onMove={(model) => setMoving({ model, source: target })}
              onCancelMove={cancelMove}
              onChanged={load}
              onToast={onToast}
            />
          ))}
        </>
      )}
      {moving && overview && (
        <MoveModelDialog
          model={moving.model}
          source={moving.source}
          targets={overview.targets}
          onClose={() => setMoving(null)}
          onStarted={(move) => {
            known.current.set(move.id, move.status)
            setMoves((current) => [move, ...current])
            onToast(`Moving ${move.repo_id}. It stays available the whole time.`)
          }}
        />
      )}
    </div>
  )
}
