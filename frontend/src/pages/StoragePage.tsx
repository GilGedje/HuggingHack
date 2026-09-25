import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertCircle,
  AlertTriangle,
  Building2,
  Check,
  Cloud,
  Database,
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
import type { StorageGrant, StorageOverview, StorageTarget } from '../types'
import { formatBytes, formatNumber, relativeTime } from '../utils'
import { visibilityLabel } from '../visibility'

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

function TargetSection({
  target,
  query,
  canManage,
  onChanged,
  onToast,
}: {
  target: StorageTarget
  query: string
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
      <div className="storage-model-table" role="table" aria-label={`Models in ${target.name}`}>
        <div className="storage-model-row header" role="row">
          <span role="columnheader">Model</span>
          <span role="columnheader">Size</span>
          <span role="columnheader">Files</span>
          <span role="columnheader">Parameters</span>
          <span role="columnheader">Status</span>
          <span role="columnheader">Updated</span>
        </div>
        {models.map((model) => (
          <div className="storage-model-row" role="row" key={model.repo_id}>
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
            <span role="cell">{formatBytes(model.size_bytes)}</span>
            <span role="cell">{formatNumber(model.file_count)}</span>
            <span role="cell">{model.parameter_count ? formatNumber(model.parameter_count) : '—'}</span>
            <span role="cell">
              {target.kind === 'filesystem' ? 'On disk' : model.cached ? 'Cached' : 'S3 only'}
            </span>
            <span role="cell">{relativeTime(model.modified_at)}</span>
          </div>
        ))}
        {models.length === 0 && (
          <div className="empty-compact">
            {target.models.length ? 'No models match your search.' : 'No models in this location yet.'}
          </div>
        )}
      </div>
    </section>
  )
}

export function StoragePage({ onToast }: { onToast: ToastHandler }) {
  const { can } = useAccess()
  const [overview, setOverview] = useState<StorageOverview | null>(null)
  const [error, setError] = useState('')
  const [scanning, setScanning] = useState(false)
  const [query, setQuery] = useState('')

  const load = useCallback(() => {
    setError('')
    api
      .storageTargets()
      .then(setOverview)
      .catch((reason) => setError(reason.message))
  }, [])

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

      {error && <div className="inline-error">{error}</div>}
      {!overview && !error && (
        <div className="drawer-loading">
          <LoaderCircle size={24} className="spin" /> Checking storage…
        </div>
      )}

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
              canManage={can('storage.manage')}
              onChanged={load}
              onToast={onToast}
            />
          ))}
        </>
      )}
    </div>
  )
}
